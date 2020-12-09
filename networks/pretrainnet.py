from .commons import PretrainNet
import torchvision
from .gan.mobilefacenet import MobileFaceNet
from .regress import Res18landmarkNet
from networks import NETWORKS, PRETRAINEDS
import torch
from datamodules.dsfunction import denormalize, normalize
import torch.functional as F
import numpy as np
import torch.nn as nn
import torch.nn.functional as nf
from utils.terminfo import ERROR
from typing import List, Tuple, Dict, Optional
import types


@PRETRAINEDS.register()
class ResNetPreTrained(PretrainNet):
  def __init__(self):
    super().__init__()
    self.res = torchvision.models.resnet18(pretrained=True)

  def _forward_impl(self, x):
    # See note [TorchScript super()]
    x = self.res.conv1(x)
    x = self.res.bn1(x)
    x = self.res.relu(x)
    x = self.res.maxpool(x)

    x = self.res.layer1(x)
    x = self.res.layer2(x)
    x = self.res.layer3(x)
    x = self.res.layer4(x)

    # x = self.avgpool(x)
    # x = torch.flatten(x, 1)
    # x = self.fc(x)
    return x


@PRETRAINEDS.register()
class VGGPreTrained(PretrainNet):
  def __init__(self):
    """pytorch vgg pretrained net

    Args:
        output_index (int, optional): output layers index. Defaults to 26.
        NOTE the finally output layer name is `output_index-1`
        ```
          (0): Conv2d (1): ReLU
          (2): Conv2d (3): ReLU
          (4): MaxPool2d
          (5): Conv2d (6): ReLU
          (7): Conv2d (8): ReLU
          (9): MaxPool2d
          (10): Conv2d (11): ReLU
          (12): Conv2d (13): ReLU
          (14): Conv2d (15): ReLU
          (16): Conv2d (17): ReLU
          (18): MaxPool2d
          (19): Conv2d (20): ReLU
          (21): Conv2d (22): ReLU
          (23): Conv2d (24): ReLU
          (25): Conv2d (26): ReLU
          (27): MaxPool2d
          (28): Conv2d (29): ReLU
          (30): Conv2d (31): ReLU
          (32): Conv2d (33): ReLU
          (34): Conv2d (35): ReLU
          (36): MaxPool2d
        ```
    """
    super().__init__()
    vgg = torchvision.models.vgg19(pretrained=True)
    self.features = vgg.features
    del vgg
    mean = torch.tensor([0.485, 0.456, 0.406])
    std = torch.tensor([0.229, 0.224, 0.225])
    mean = mean[None, :, None, None]
    std = std[None, :, None, None]
    self.register_buffer('mean', mean)
    self.register_buffer('std', std)
    self.vgg_normalize = lambda x: normalize(x, self.mean, self.std)

  def _process(self, x):
    # NOTE 图像范围为[-1~1]，先denormalize到0-1再归一化
    return self.vgg_normalize(denormalize(x))

  def setup(self, device: torch.device):
    self.freeze()

  def _forward_impl(self, x):
    x = self._process(x)
    # See note [TorchScript super()]
    # NOTE get output with out relu activation
    x = self.features(x)
    # x = self.avgpool(x)
    # x = torch.flatten(x, 1)
    # x = self.fc(x)
    return x

  def get_name_list(self):
    named_children = list(self.features.named_children())
    return [name + '_' + mod._get_name() for name, mod in named_children]


@PRETRAINEDS.register()
class FacePreTrained(PretrainNet):
  def __init__(self, weights_path):
    super().__init__()
    self.model = MobileFaceNet(512)
    try:
      self.model.load_state_dict(torch.load(weights_path))
    except FileNotFoundError as e:
      print(ERROR, "weights_path:", weights_path,
            'does not exits!, if you want to training must download pretrained weights')

  def infer(self, batch_tensor):
    # crop face
    h, w = batch_tensor.shape[2:]
    top = int(h / 2.1 * (0.8 - 0.33))
    bottom = int(h - (h / 2.1 * 0.3))
    size = bottom - top
    left = int(w / 2 - size / 2)
    right = left + size
    batch_tensor = batch_tensor[:, :, top: bottom, left: right]

    batch_tensor = nf.interpolate(
        batch_tensor, size=[112, 112], mode='bilinear', align_corners=True)

    features = self.model(batch_tensor)
    return features

  def cosine_distance(self, batch_tensor1, batch_tensor2):
    feature1 = self.infer(batch_tensor1)
    feature2 = self.infer(batch_tensor2)
    return 1 - torch.cosine_similarity(feature1, feature2)

  def forward(self, batch_tensor1, batch_tensor2):
    return self.cosine_distance(batch_tensor1, batch_tensor2)


@PRETRAINEDS.register()
class FaceLandmarkPreTrained(PretrainNet):
  def __init__(self, name='Res18landmarkNet', weights_path='models/facelandmark_full.pth'):
    super().__init__()
    self.model: Res18landmarkNet = NETWORKS.get(name)(landmark_num=5, pretrained=False)
    self._load_weight(weights_path)

  def _load_weight(self, weights_path):
    try:
      self.model.load_state_dict(torch.load(weights_path))
    except FileNotFoundError as e:
      print(ERROR, "weights_path:", weights_path,
            'does not exits!, if you want to training must download pretrained weights')

  def _process(self, x):
    # NOTE 图像范围为[-1~1]，先denormalize到0-1再归一化
    return self.normalize(denormalize(x))

  def setup(self, device: torch.device):
    mean: torch.Tensor = torch.tensor([0.485, 0.456, 0.406], device=device)
    std = torch.tensor([0.229, 0.224, 0.225], device=device)
    mean = mean[None, :, None, None]
    std = std[None, :, None, None]
    self.normalize = lambda x: normalize(x, mean, std)
    self.freeze()

  def _forward_impl(self, x):
    x = self._process(x)
    x = self.model(x)
    pred = torch.sigmoid(x)
    # NOTE get output [batch,n_landmark,2]
    return torch.reshape(pred, (-1, 5, 2))


@PRETRAINEDS.register()
class AttentionFaceLandmarkPreTrained(FaceLandmarkPreTrained):
  def _forward_impl(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
    """ attention network forward
    NOTE change heatmap to 0-1

    Args:
        x (torch.Tensor): input

    Returns:
        Tuple[torch.Tensor, torch.Tensor]: pred, heatmap
    """
    x = self._process(x)
    x, heatmap = self.model(x)
    pred = torch.sigmoid(x)
    # NOTE get output [batch,n_landmark,2]
    pred = torch.reshape(pred, (-1, 5, 2))

    # NOTE process heatmap
    heatmap: torch.Tensor
    heatmap = heatmap - heatmap.min()
    heatmap = (1 - (heatmap / heatmap.max()))
    return pred, heatmap


@PRETRAINEDS.register()
class VGGCaffePreTrained(PretrainNet):
  cfg = [64, 64, 'M', 128, 128, 'M', 256, 256, 256, 256,
         'M', 512, 512, 512, 512, 'M', 512, 512, 512, 512, 'M']

  def __init__(self, weights_path: str = 'models/vgg19.npy') -> None:
    super().__init__()
    try:
      data_dict: dict = np.load(weights_path, encoding='latin1', allow_pickle=True).item()
      self.features = self.make_layers(self.cfg, data_dict)
      del data_dict
    except FileNotFoundError as e:
      print(ERROR, "weights_path:", weights_path,
            'does not exits!, if you want to training must download pretrained weights')
    mean = torch.tensor([103.939, 116.779, 123.68])[None, :, None, None]
    self.register_buffer('mean', mean)
    self.vgg_normalize = lambda x: x - self.mean

  def _process(self, x):
    # NOTE 图像范围为[-1~1]，先denormalize到0-1再归一化
    rgb = denormalize(x) * 255  # to 255
    bgr = rgb[:, [2, 1, 0], :, :]  # rgb to bgr
    return self.vgg_normalize(bgr)  # vgg norm

  def _forward_impl(self, x):
    x = self._process(x)
    # NOTE get output with out relu activation
    x = self.features(x)
    return x

  @staticmethod
  def get_conv_filter(data_dict, name):
    return data_dict[name][0]

  @staticmethod
  def get_bias(data_dict, name):
    return data_dict[name][1]

  @staticmethod
  def get_fc_weight(data_dict, name):
    return data_dict[name][0]

  def make_layers(self, cfg, data_dict, batch_norm=False) -> nn.Sequential:
    layers = []
    in_channels = 3
    block = 1
    number = 1
    for v in cfg:
      if v == 'M':
        layers += [nn.MaxPool2d(kernel_size=2, stride=2)]
        block += 1
        number = 1
      else:
        conv2d = nn.Conv2d(in_channels, v, kernel_size=3, padding=1)
        with torch.no_grad():
          """ set value """
          weight = torch.FloatTensor(self.get_conv_filter(data_dict, f'conv{block}_{number}'))
          weight = weight.permute((3, 2, 0, 1))
          bias = torch.FloatTensor(self.get_bias(data_dict, f'conv{block}_{number}'))
          conv2d.weight.set_(weight)
          conv2d.bias.set_(bias)
        number += 1
        if batch_norm:
          layers += [conv2d, nn.BatchNorm2d(v), nn.ReLU(inplace=True)]
        else:
          layers += [conv2d, nn.ReLU(inplace=True)]
        in_channels = v

    return nn.Sequential(*layers)

  def get_name_list(self):
    named_children = list(self.features.named_children())
    return [name + '_' + mod._get_name() for name, mod in named_children]
