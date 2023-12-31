import torch
import torch.nn as nn
import torch.nn.functional as F
from .resnet18 import Resnet18
from Training.ptsemseg.utils import split_bise_state_dict
from Training.ptsemseg.models.td4_bise.bisenet_4p import bisenet_4p
import logging
import os
from Training.ptsemseg.loss.loss import OhemCELoss2D, SegmentationLosses
from .transformer import Encoding, Attention

up_kwargs = {'mode': 'bilinear', 'align_corners': True}
logger = logging.getLogger("ptsemseg")


class td4_bise(nn.Module):
    def __init__(self,
                 nclass=6,
                 norm_layer=nn.BatchNorm2d,
                 backbone='resnet34',
                 dilated=True,
                 aux=True,
                 multi_grid=True,
                 loss_fn=None,
                 path_num=None,
                 mdl_path=None,
                 teacher=None
                 ):
        super(td4_bise, self).__init__()

        self.bise_path = mdl_path
        self.loss_fn = loss_fn
        self.path_num = path_num
        self.norm_layer = norm_layer
        self._up_kwargs = up_kwargs
        self.nclass = nclass

        self.backbone = backbone
        assert (backbone == 'resnet18')
        assert (path_num == 4)

        if backbone == 'resnet18':
            ResNet_ = Resnet18()
            deep_base = False
            self.expansion = 1
        else:
            raise RuntimeError("Four branch model only support ResNet18")

        self.pretrained1 = ResNet_(pretrained=True, dilated=dilated, multi_grid=multi_grid,
                                   deep_base=deep_base, norm_layer=norm_layer)
        self.pretrained2 = ResNet_(pretrained=True, dilated=dilated, multi_grid=multi_grid,
                                   deep_base=deep_base, norm_layer=norm_layer)
        self.pretrained3 = ResNet_(pretrained=True, dilated=dilated, multi_grid=multi_grid,
                                   deep_base=deep_base, norm_layer=norm_layer)
        self.pretrained4 = ResNet_(pretrained=True, dilated=dilated, multi_grid=multi_grid,
                                   deep_base=deep_base, norm_layer=norm_layer)

        self.bise1 = BiSeSubNet(256, 256)
        self.bise2 = BiSeSubNet(256, 256)
        self.bise3 = BiSeSubNet(256, 256)
        self.bise4 = BiSeSubNet(256, 256)

        self.enc1 = Encoding(512 * self.expansion, 64, 512 * self.expansion, norm_layer)
        self.enc2 = Encoding(512 * self.expansion, 64, 512 * self.expansion, norm_layer)
        self.enc3 = Encoding(512 * self.expansion, 64, 512 * self.expansion, norm_layer)
        self.enc4 = Encoding(512 * self.expansion, 64, 512 * self.expansion, norm_layer)

        self.atn1_2 = Attention(512 * self.expansion, 64, norm_layer)
        self.atn1_3 = Attention(512 * self.expansion, 64, norm_layer)
        self.atn1_4 = Attention(512 * self.expansion, 64, norm_layer)

        self.atn2_1 = Attention(512 * self.expansion, 64, norm_layer)
        self.atn2_3 = Attention(512 * self.expansion, 64, norm_layer)
        self.atn2_4 = Attention(512 * self.expansion, 64, norm_layer)

        self.atn3_1 = Attention(512 * self.expansion, 64, norm_layer)
        self.atn3_2 = Attention(512 * self.expansion, 64, norm_layer)
        self.atn3_4 = Attention(512 * self.expansion, 64, norm_layer)

        self.atn4_1 = Attention(512 * self.expansion, 64, norm_layer)
        self.atn4_2 = Attention(512 * self.expansion, 64, norm_layer)
        self.atn4_3 = Attention(512 * self.expansion, 64, norm_layer)

        self.layer_norm1 = Layer_Norm([97, 193])
        self.layer_norm2 = Layer_Norm([97, 193])
        self.layer_norm3 = Layer_Norm([97, 193])
        self.layer_norm4 = Layer_Norm([97, 193])

        self.head1 = FCNHead(512 * self.expansion * 1, nclass, norm_layer, chn_down=4)
        self.head2 = FCNHead(512 * self.expansion * 1, nclass, norm_layer, chn_down=4)
        self.head3 = FCNHead(512 * self.expansion * 1, nclass, norm_layer, chn_down=4)
        self.head4 = FCNHead(512 * self.expansion * 1, nclass, norm_layer, chn_down=4)

        if aux:
            self.auxlayer1 = FCNHead(256 * self.expansion, nclass, norm_layer)
            self.auxlayer2 = FCNHead(256 * self.expansion, nclass, norm_layer)
            self.auxlayer3 = FCNHead(256 * self.expansion, nclass, norm_layer)
            self.auxlayer4 = FCNHead(256 * self.expansion, nclass, norm_layer)

        # self.pretrained_init_2p()
        self.pretrained_init()
        self.KLD = nn.KLDivLoss()
        self.get_params()
        self.teacher = teacher

    def forward_path_1(self, f_img):
        f1_img = f_img[0]
        f2_img = f_img[1]
        f3_img = f_img[2]
        f4_img = f_img[3]

        _, _, h, w = f4_img.size()

        c3_1, c4_1 = self.pretrained1(f4_img)
        c3_2, c4_2 = self.pretrained2(f1_img)
        c3_3, c4_3 = self.pretrained3(f2_img)
        c3_4, c4_4 = self.pretrained4(f3_img)

        z1 = self.bise1(c4_1)
        z2 = self.bise2(c4_2)
        z3 = self.bise3(c4_3)
        z4 = self.bise4(c4_4)

        v1, q1 = self.enc1(z1, pre=False)
        k_2, v_2, _ = self.enc2(z2, pre=True, start=True)
        k_3, v_3, q_3 = self.enc3(z3, pre=True)
        k_4, v_4, q_4 = self.enc4(z4, pre=True)

        v_3_ = self.atn1_2(k_2, v_2, q_3, fea_size=None)
        v_4_ = self.atn1_3(k_3, v_3_ + v_3, q_4, fea_size=None)
        atn_1 = self.atn1_4(k_4, v_4_ + v_4, q1, fea_size=z4.size())

        out1 = self.head1(self.layer_norm1(atn_1 + v1))
        out1_sub = self.head1(self.layer_norm1(v1))

        outputs1 = F.interpolate(out1, (h, w), **self._up_kwargs)
        outputs1_sub = F.interpolate(out1_sub, (h, w), **self._up_kwargs)

        if self.training:
            #############Knowledge-distillation###########
            self.teacher.eval()
            T_logit_1234, T_logit_1, T_logit_2, T_logit_3, T_logit_4 = self.teacher(f4_img)
            T_logit_1234 = T_logit_1234.detach()
            T_logit_1 = T_logit_1.detach()
            T_logit_2 = T_logit_2.detach()
            T_logit_3 = T_logit_3.detach()
            T_logit_4 = T_logit_4.detach()

            KD_loss1 = self.KLDive_loss(out1, T_logit_1234) + 0.5 * self.KLDive_loss(out1_sub, T_logit_1)
            auxout1 = self.auxlayer1(c3_1)
            auxout1 = F.interpolate(auxout1, (h, w), **self._up_kwargs)
            return outputs1, outputs1_sub, auxout1, KD_loss1
        else:
            return outputs1

    def forward_path_2(self, f_img):
        f1_img = f_img[0]
        f2_img = f_img[1]
        f3_img = f_img[2]
        f4_img = f_img[3]

        _, _, h, w = f4_img.size()

        c3_1, c4_1 = self.pretrained1(f3_img)
        c3_2, c4_2 = self.pretrained2(f4_img)
        c3_3, c4_3 = self.pretrained3(f1_img)
        c3_4, c4_4 = self.pretrained4(f2_img)

        z1 = self.bise1(c4_1)
        z2 = self.bise2(c4_2)
        z3 = self.bise3(c4_3)
        z4 = self.bise4(c4_4)

        k_1, v_1, q_1 = self.enc1(z1, pre=True)
        v2, q2 = self.enc2(z2, pre=False)
        k_3, v_3, _ = self.enc3(z3, pre=True, start=True)
        k_4, v_4, q_4 = self.enc4(z4, pre=True)

        v_4_ = self.atn2_3(k_3, v_3, q_4, fea_size=None)
        v_1_ = self.atn2_4(k_4, v_4_ + v_4, q_1, fea_size=None)
        atn_2 = self.atn2_1(k_1, v_1_ + v_1, q2, fea_size=z4.size())

        ############### SegHead ##############
        out2 = self.head2(self.layer_norm2(atn_2 + v2))
        out2_sub = self.head2(self.layer_norm2(v2))

        outputs2 = F.interpolate(out2, (h, w), **self._up_kwargs)
        outputs2_sub = F.interpolate(out2_sub, (h, w), **self._up_kwargs)

        if self.training:
            #############Knowledge-distillation###########
            self.teacher.eval()
            T_logit_1234, T_logit_1, T_logit_2, T_logit_3, T_logit_4 = self.teacher(f4_img)
            T_logit_1234 = T_logit_1234.detach()
            T_logit_1 = T_logit_1.detach()
            T_logit_2 = T_logit_2.detach()
            T_logit_3 = T_logit_3.detach()
            T_logit_4 = T_logit_4.detach()

            KD_loss2 = self.KLDive_loss(out2, T_logit_1234) + 0.5 * self.KLDive_loss(out2_sub, T_logit_2)
            auxout2 = self.auxlayer2(c3_2)
            auxout2 = F.interpolate(auxout2, (h, w), **self._up_kwargs)
            return outputs2, outputs2_sub, auxout2, KD_loss2
        else:
            return outputs2

    def forward_path_3(self, f_img):
        f1_img = f_img[0]
        f2_img = f_img[1]
        f3_img = f_img[2]
        f4_img = f_img[3]

        _, _, h, w = f4_img.size()

        c3_1, c4_1 = self.pretrained1(f2_img)
        c3_2, c4_2 = self.pretrained2(f3_img)
        c3_3, c4_3 = self.pretrained3(f4_img)
        c3_4, c4_4 = self.pretrained4(f1_img)

        z1 = self.bise1(c4_1)
        z2 = self.bise2(c4_2)
        z3 = self.bise3(c4_3)
        z4 = self.bise4(c4_4)

        k_1, v_1, q_1 = self.enc1(z1, pre=True)
        k_2, v_2, q_2 = self.enc2(z2, pre=True)
        v3, q3 = self.enc3(z3, pre=False)
        k_4, v_4, _ = self.enc4(z4, pre=True, start=True)

        v_1_ = self.atn3_4(k_4, v_4, q_1, fea_size=None)
        v_2_ = self.atn3_1(k_1, v_1_ + v_1, q_2, fea_size=None)
        atn_3 = self.atn3_2(k_2, v_2_ + v_2, q3, fea_size=z4.size())

        ############### SegHead ##############
        out3 = self.head3(self.layer_norm3(atn_3 + v3))
        out3_sub = self.head3(self.layer_norm3(v3))

        outputs3 = F.interpolate(out3, (h, w), **self._up_kwargs)
        outputs3_sub = F.interpolate(out3_sub, (h, w), **self._up_kwargs)

        if self.training:
            #############Knowledge-distillation###########
            self.teacher.eval()
            T_logit_1234, T_logit_1, T_logit_2, T_logit_3, T_logit_4 = self.teacher(f4_img)
            T_logit_1234 = T_logit_1234.detach()
            T_logit_1 = T_logit_1.detach()
            T_logit_2 = T_logit_2.detach()
            T_logit_3 = T_logit_3.detach()
            T_logit_4 = T_logit_4.detach()

            KD_loss3 = self.KLDive_loss(out3, T_logit_1234) + 0.5 * self.KLDive_loss(out3_sub, T_logit_3)
            auxout3 = self.auxlayer3(c3_3)
            auxout3 = F.interpolate(auxout3, (h, w), **self._up_kwargs)
            return outputs3, outputs3_sub, auxout3, KD_loss3
        else:
            return outputs3

    def forward_path_4(self, f_img):
        f1_img = f_img[0]
        f2_img = f_img[1]
        f3_img = f_img[2]
        f4_img = f_img[3]

        _, _, h, w = f4_img.size()

        c3_1, c4_1 = self.pretrained1(f1_img)
        c3_2, c4_2 = self.pretrained2(f2_img)
        c3_3, c4_3 = self.pretrained3(f3_img)
        c3_4, c4_4 = self.pretrained4(f4_img)

        z1 = self.bise1(c4_1)
        z2 = self.bise2(c4_2)
        z3 = self.bise3(c4_3)
        z4 = self.bise4(c4_4)

        k_1, v_1, _ = self.enc1(z1, pre=True, start=True)
        k_2, v_2, q_2 = self.enc2(z2, pre=True)
        k_3, v_3, q_3 = self.enc3(z3, pre=True)
        v4, q4 = self.enc4(z4, pre=False)

        v_2_ = self.atn4_1(k_1, v_1, q_2, fea_size=None)
        v_3_ = self.atn4_2(k_2, v_2_ + v_2, q_3, fea_size=None)
        atn_4 = self.atn4_3(k_3, v_3_ + v_3, q4, fea_size=z4.size())
        # atn_4 = self.atn3(k_3, v_3, q4, fea_size=z4.size())

        ############### SegHead ##############
        out4 = self.head4(self.layer_norm4(atn_4 + v4))
        out4_sub = self.head4(self.layer_norm4(v4))

        outputs4 = F.interpolate(out4, (h, w), **self._up_kwargs)
        outputs4_sub = F.interpolate(out4_sub, (h, w), **self._up_kwargs)

        if self.training:
            #############Knowledge-distillation###########
            self.teacher.eval()
            T_logit_1234, T_logit_1, T_logit_2, T_logit_3, T_logit_4 = self.teacher(f4_img)
            T_logit_1234 = T_logit_1234.detach()
            T_logit_1 = T_logit_1.detach()
            T_logit_2 = T_logit_2.detach()
            T_logit_3 = T_logit_3.detach()
            T_logit_4 = T_logit_4.detach()

            KD_loss4 = self.KLDive_loss(out4, T_logit_1234) + 0.5 * self.KLDive_loss(out4_sub, T_logit_4)
            auxout4 = self.auxlayer4(c3_4)
            auxout4 = F.interpolate(auxout4, (h, w), **self._up_kwargs)
            return outputs4, outputs4_sub, auxout4, KD_loss4
        else:
            return outputs4

    def forward(self, f_img, lbl=None, pos_id=None):
        if pos_id == 0:
            outputs = self.forward_path1(f_img)
        elif pos_id == 1:
            outputs = self.forward_path2(f_img)
        elif pos_id == 2:
            outputs = self.forward_path3(f_img)
        elif pos_id == 3:
            outputs = self.forward_path4(f_img)
        else:
            raise RuntimeError("Only Four Paths.")

        if self.training:
            outputs_, outputs_sub, auxout, KD_loss = outputs
            loss = self.loss_fn(outputs_, lbl) + \
                   0.5 * self.loss_fn(outputs_sub, lbl) + \
                   0.1 * self.loss_fn(auxout, lbl) + \
                   1 * KD_loss

            return loss
        else:
            # outputs = F.interpolate(outputs, (1024, 2048), **self._up_kwargs)
            return outputs

    def KLDive_loss(self, Q, P):
        # Info_gain - KL Divergence
        # P is the target
        # Q is the computed one
        temp = 1
        P = F.softmax(P / temp, dim=1) + 1e-8
        Q = F.softmax(Q / temp, dim=1) + 1e-8

        KLDiv = (P * (P / Q).log()).sum(1) * (temp ** 2)
        return KLDiv.mean()

    def get_params(self):
        wd_params, nowd_params, lr_mul_wd_params, lr_mul_nowd_params = [], [], [], []
        for name, child in self.named_children():
            if isinstance(child, (OhemCELoss2D, SegmentationLosses, bisenet_4p, nn.KLDivLoss)):
                continue
            child_wd_params, child_nowd_params = child.get_params()
            if isinstance(child, (Encoding, Attention, BiSeSubNet, FCNHead, Layer_Norm)):
                lr_mul_wd_params += child_wd_params
                lr_mul_nowd_params += child_nowd_params
            else:
                wd_params += child_wd_params
                nowd_params += child_nowd_params
        return wd_params, nowd_params, lr_mul_wd_params, lr_mul_nowd_params

    def pretrained_init(self):
        if self.psp_path is not None:
            if os.path.isfile(self.psp_path):
                logger.info("Initializaing sub networks with pretrained '{}'".format(self.psp_path))
                print("Initializaing sub networks with pretrained '{}'".format(self.psp_path))
                model_state = torch.load(self.psp_path)
                backbone_state, psp_state, head_state1, head_state2, _, _, auxlayer_state = split_bise_state_dict(model_state,
                                                                                                           self.path_num // 2)
                self.pretrained1.load_state_dict(backbone_state, strict=True)
                self.pretrained2.load_state_dict(backbone_state, strict=True)
                self.pretrained3.load_state_dict(backbone_state, strict=True)
                self.pretrained4.load_state_dict(backbone_state, strict=True)
                self.bise1.load_state_dict(psp_state, strict=True)
                self.bise2.load_state_dict(psp_state, strict=True)
                self.bise3.load_state_dict(psp_state, strict=True)
                self.bise4.load_state_dict(psp_state, strict=True)
                self.head1.load_state_dict(head_state1, strict=False)
                self.head2.load_state_dict(head_state2, strict=False)
                self.head3.load_state_dict(head_state1, strict=False)
                self.head4.load_state_dict(head_state2, strict=False)
                self.auxlayer1.load_state_dict(auxlayer_state, strict=True)
                self.auxlayer2.load_state_dict(auxlayer_state, strict=True)
                self.auxlayer3.load_state_dict(auxlayer_state, strict=True)
                self.auxlayer4.load_state_dict(auxlayer_state, strict=True)
            else:
                logger.info("No pretrained found at '{}'".format(self.psp_path))


class FCNHead(nn.Module):
    def __init__(self, in_channels, out_channels, norm_layer, up_kwargs={}, chn_down=4):
        super(FCNHead, self).__init__()

        inter_channels = in_channels // chn_down

        self._up_kwargs = up_kwargs
        self.norm_layer = norm_layer
        self.conv5 = nn.Sequential(nn.Conv2d(in_channels, inter_channels, 3, padding=1, bias=False),
                                   norm_layer(inter_channels),
                                   nn.ReLU(),
                                   nn.Dropout2d(0.1, False),
                                   nn.Conv2d(inter_channels, out_channels, 1))
        self.init_weight()

    def forward(self, x):
        return self.conv5(x)

    def init_weight(self):
        for ly in self.children():
            if isinstance(ly, nn.Sequential):
                for lz in ly:
                    if isinstance(lz, nn.Conv2d):
                        nn.init.kaiming_normal_(lz.weight, a=1)
                        if not lz.bias is None: nn.init.constant_(lz.bias, 0)
            if isinstance(ly, nn.Conv2d):
                nn.init.kaiming_normal_(ly.weight, a=1)
                if not ly.bias is None: nn.init.constant_(ly.bias, 0)

    def get_params(self):
        wd_params, nowd_params = [], []
        for name, module in self.named_modules():
            if isinstance(module, (nn.Linear, nn.Conv2d)):
                wd_params.append(module.weight)
                if not module.bias is None:
                    nowd_params.append(module.bias)
            elif isinstance(module, self.norm_layer):
                nowd_params += list(module.parameters())
        return wd_params, nowd_params


class Layer_Norm(nn.Module):
    def __init__(self, shape):
        super(Layer_Norm, self).__init__()
        self.ln = nn.LayerNorm(shape)

    def forward(self, x):
        return self.ln(x)

    def get_params(self):
        wd_params, nowd_params = [], []
        for name, module in self.named_modules():
            if isinstance(module, (nn.Linear, nn.Conv2d)):
                wd_params.append(module.weight)
                if not module.bias is None:
                    nowd_params.append(module.bias)
            elif isinstance(module, nn.LayerNorm):
                nowd_params += list(module.parameters())
        return wd_params, nowd_params


class BiSeSubNet(nn.Module):
    def __init__(self, in_chan, out_chan, *args, **kwargs):
        super(BiSeSubNet, self).__init__()
        self.convblk = bisenet_4p.ConvBNReLU(in_chan, out_chan, ks=1, stride=1, padding=0)
        self.conv1 = nn.Conv2d(out_chan,
                               out_chan // 4,
                               kernel_size=1,
                               stride=1,
                               padding=0,
                               bias=False)
        self.conv2 = nn.Conv2d(out_chan // 4,
                               out_chan,
                               kernel_size=1,
                               stride=1,
                               padding=0,
                               bias=False)
        self.relu = nn.ReLU(inplace=True)
        self.sigmoid = nn.Sigmoid()
        self.init_weight()

        self.sp = bisenet_4p.SpatialPath()
        self.cp = bisenet_4p.ContextPath()

    def forward(self, x):
        fsp = self.sp(x)
        fcp = self.cp(x)

        fcat = torch.cat([fsp, fcp], dim=1)
        feat = self.convblk(fcat)
        atten = torch.mean(feat, dim=(2, 3), keepdim=True)
        atten = self.conv1(atten)
        atten = self.relu(atten)
        atten = self.conv2(atten)
        atten = self.sigmoid(atten)
        feat_atten = torch.mul(feat, atten)
        feat_out = feat_atten + feat
        return feat_out

    def init_weight(self):
        for ly in self.children():
            if isinstance(ly, nn.Conv2d):
                nn.init.kaiming_normal_(ly.weight, a=1)
                if not ly.bias is None:
                    nn.init.constant_(ly.bias, 0)

    def get_params(self):
        wd_params, nowd_params = [], []
        for name, module in self.named_modules():
            if isinstance(module, (nn.Linear, nn.Conv2d)):
                wd_params.append(module.weight)
                if not module.bias is None:
                    nowd_params.append(module.bias)
            elif isinstance(module, nn.modules.batchnorm._BatchNorm):
                nowd_params += list(module.parameters())
        return wd_params, nowd_params
