#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Mar  5 15:37:59 2019

@author: ubuntu
"""

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from mmdet.core import multiclass_nms

from utils.anchor_generator import AnchorGenerator
from utils.anchor_target import anchor_target
from utils.multi_apply import multi_apply  
from utils.bbox_reg import delta2bbox
from model.weight_init import kaiming_normal_init
from model.losses import weighted_smoothl1
from utils.registry_build import registered


@registered.register_module
class M2detHead(nn.Module):
    """M2detHead主要完成3件事：生成anchors, 处理feat maps，计算loss
    1. anchors的生成：输入anchor跟原图的尺寸比例
       先基于anchor的尺寸比例乘以img尺寸得到min_size, max_size，然后基于strides
       计算anchor在cell的中心点坐标ctx,cty, 然后定义scales=[1,sqrt(max_size/min_size)]
       以及定义ratio=[1,2,1/2,3,1/3], 取scale=1的5种ratio，然后区scale=sqrt(max_size/min_size)的1种ratio=1
    """
    def __init__(self,
                 input_size = 512,      # 相同保留
                 planes = 256,           # 代表tums最终输出层数
                 num_levels = 8,
                 num_classes = 81,
                 anchor_strides = [8, 16, 32, 64, 128, 256],  # 代表特征图的缩放比例，也就是每个特征图上cell对应的原图大小，也就是原图所要布置anchor的尺寸空间(可以此计算ctx,cty)
                 size_pattern = [0.06, 0.15, 0.33, 0.51, 0.69, 0.87, 1.05],  # 代表anchors尺寸跟img的比例, 前6个数是min_size比例，后6个数是max_size比例，可以此计算anchor最小最大尺寸
                 size_featmaps = [(64,64), (32,32), (16,16), (8,8), (4,4), (2,2)],
                 anchor_ratio_range = ([2, 3], [2, 3], [2, 3], [2, 3], [2, 3], [2, 3]),
                 target_means=(.0, .0, .0, .0),
                 target_stds=(1.0, 1.0, 1.0, 1.0),
                 **kwargs):  # 这里的2代表了2和1/2, 而3代表了3和1/3，可以此计算每个cell的anchor个数：2个方框+4个ratio=6个
        super().__init__()
        self.featmap_sizes = size_featmaps
        self.anchor_strides = anchor_strides
        self.anchor_ratios = anchor_ratio_range
        self.num_classes = num_classes
        self.target_means = target_means
        self.target_stds = target_stds
        
        # create m2det head layers
        reg_convs = []
        cls_convs = []
        for i in range(len(size_featmaps)):
            reg_convs.append(
                nn.Conv2d(
                    planes * num_levels,
                    4 * 6,
                    kernel_size=3,
                    stride=1,
                    padding=1))
            cls_convs.append(
                nn.Conv2d(
                    planes * num_levels,
                    num_classes * 6,
                    kernel_size=3,
                    stride=1,
                    padding=1))
        self.reg_convs = nn.ModuleList(reg_convs)
        self.cls_convs = nn.ModuleList(cls_convs)
        
        # generate anchors
        if input_size == 512:
            min_ratios = size_pattern[:-1]
            max_ratios = size_pattern[1:]
            
            min_sizes = [min_ratios[i] * input_size for i in range(len(min_ratios))]
            max_sizes = [max_ratios[i] * input_size for i in range(len(max_ratios))]
            
            
            
        
        self.anchor_generators = []
        for k in range(len(self.anchor_strides)):
            base_size = min_sizes[k]
            stride = self.anchor_strides[k]
            ctr = ((stride - 1) / 2., (stride - 1) / 2.)
            scales = [1., np.sqrt(max_sizes[k] / min_sizes[k])]  # 以base_size为第一个anchor，即scale=1，再以
            ratios = [1.]
            for r in self.anchor_ratios[k]:
                ratios += [1 / r, r]
            # scales (2,) and ratios (5,), if scale_major=False, (1,5)*(2,1)->(2,5)*(2,5)->(2,5)
            anchor_generator = AnchorGenerator(
                base_size, scales, ratios, scale_major=False, ctr=ctr)
            
            # for m2det, there are 6 anchors for each cells, no matter in any featmap cell
            anchor_generator.base_anchors = anchor_generator.base_anchors[:6]  # 取前6个anchors(对应scale=1的5种ratios + scale=sqrt(max_size/min_size)的1种ratio)
#            indices = list(range(len(ratios)))
#            indices.insert(1, len(indices))
#            anchor_generator.base_anchors = torch.index_select(
#                anchor_generator.base_anchors, 0, torch.LongTensor(indices))
            self.anchor_generators.append(anchor_generator)
    
    def init_weights(self):
        
        def weights_init(m):
            for key in m.state_dict():
                if key.split('.')[-1] == 'weight':
                    if 'conv' in key:
                        kaiming_normal_init(m.state_dict()[key], mode='fan_out')
                    if 'bn' in key:
                        m.state_dict()[key][...] = 1
                elif key.split('.')[-1] == 'bias':
                    m.state_dict()[key][...] = 0   
        
        self.cls_convs.apply(weights_init)
        self.reg_convs.apply(weights_init)
    
    def forward(self, feats):
        """return the output features of MLFPN
        Args:
            feats(tensor): (6,)feats with (2,2048,64,64),(2,2048,32,32),(2,2048,16,16),(2,2048,8,8),(2,2048,4,4),(2,2048,2,2)
        Returns:
            bbox_preds(tensor): (6,)feats with (b,24,64,64),(b,24,32,32),(b,24,16,16),(b,24,8,8),(b,24,4,4),(b,24,2,2)
            cls_scores(tensor): (6,)feats with (b,486,64,64),(b,486,32,32),(b,486,16,16),(b,486,8,8),(b,486,4,4),(b,486,2,2)
        """
        bbox_preds = []
        cls_scores = []
        for (feat, reg_conv, cls_conv) in zip(feats, self.reg_convs, self.cls_convs):
            bbox_preds.append(reg_conv(feat))
            cls_scores.append(cls_conv(feat))
        return cls_scores, bbox_preds
    
    def get_anchors(self, featmap_sizes, img_metas):
        """Get anchors according to feature map sizes.
        Args:
            featmap_sizes (list[tuple]): Multi-level feature map sizes.
            img_metas (list[dict]): Image meta info.

        Returns:
            tuple: anchors of each image, valid flags of each image
        """
        num_imgs = len(img_metas)
        num_levels = len(featmap_sizes)

        # since feature map sizes of all images are the same, we only compute
        # anchors for one time
        multi_level_anchors = []
        for i in range(num_levels):
            anchors = self.anchor_generators[i].grid_anchors(
                featmap_sizes[i], self.anchor_strides[i])
            multi_level_anchors.append(anchors)
        anchor_list = [multi_level_anchors for _ in range(num_imgs)]

        # for each image, we compute valid flags of multi level anchors
        valid_flag_list = []
        for img_id, img_meta in enumerate(img_metas):
            multi_level_flags = []
            for i in range(num_levels):
                anchor_stride = self.anchor_strides[i]
                feat_h, feat_w = featmap_sizes[i]
                h, w, _ = img_meta['pad_shape']
                valid_feat_h = min(int(np.ceil(h / anchor_stride)), feat_h)
                valid_feat_w = min(int(np.ceil(w / anchor_stride)), feat_w)
                flags = self.anchor_generators[i].valid_flags(
                    (feat_h, feat_w), (valid_feat_h, valid_feat_w))
                multi_level_flags.append(flags)
            valid_flag_list.append(multi_level_flags)

        return anchor_list, valid_flag_list
                
    def loss_single(self, cls_score, bbox_pred, labels, label_weights,
                    bbox_targets, bbox_weights, num_total_samples, cfg):
        """return one img loss
        Args:
            cls_score(tensor): (m, num_class) for all bbox in one img
            bbox_pred(tensor): (m, 4) for all bbox coodinate in one img
            labels(tensor): (m,)
            label_weights(tensor): (m,)
            bbox_targets(tensor): (m, 4)
            bbox_weights(tensor): (m,)
            num_total_samples
            cfg
        """
        # loss of cls
        loss_cls_all = F.cross_entropy(cls_score, labels, reduction='none')*label_weights
        
        # hard negtive mining
        pos_inds = (labels > 0).nonzero().view(-1)
        neg_inds = (labels == 0).nonzero().view(-1)
        num_pos_samples = pos_inds.size(0)
        num_neg_samples = cfg.neg_pos_ratio * num_pos_samples
        if num_neg_samples > neg_inds.size(0):
            num_neg_samples = neg_inds.size(0)
        # only take 3*num_pos_samples topk as neg samples: topk means k max losses
        topk_loss_cls_neg, _ = loss_cls_all[neg_inds].topk(num_neg_samples)  
        loss_cls_pos = loss_cls_all[pos_inds].sum()
        loss_cls_neg = topk_loss_cls_neg.sum()
        loss_cls = (loss_cls_pos + loss_cls_neg) / num_total_samples
        
        # loss of reg
        loss_reg = weighted_smoothl1(bbox_pred, bbox_targets, bbox_weights, 
                                     beta=cfg.smoothl1_beta, 
                                     avg_factor=num_total_samples)
        return loss_cls, loss_reg
    
    def loss(self, cls_scores, bbox_preds, gt_bboxes, gt_labels, img_metas, cfg):
        """ return losses dict('loss_cls', 'loss_reg')
        Args:
            cls_scores(list): (6,) with (b,n_class*6,h,w)
            bbox_preds(list): (6,) with (b,n_cord*6,h,w)
            gt_bboxes(list): (n_img,) with (m,4)
            gt_labels(list): (n_img,) with (m,)
            img_metas(list): (n_img,) with dict()
            cfg
        """
        # get all anchors (n_img,): 
        anchor_list, valid_flag_list = self.get_anchors(self.featmap_sizes, img_metas) # anchor_list(b,) with (24576,4),(6114,4),(1536,4),(384,4),(96,4),(24,4)
        # get target (n_scale,): (2,k,4)
        cls_reg_targets = anchor_target(
            anchor_list,
            valid_flag_list,
            gt_bboxes,
            img_metas,
            self.target_means,
            self.target_stds,
            cfg,
            gt_labels_list=gt_labels,
            label_channels=1,
            sampling=False,
            unmap_outputs=False)
        
        if cls_reg_targets is None:
            return None
        (labels_list, label_weights_list, bbox_targets_list, 
         bbox_weights_list, num_total_pos, num_total_neg) = cls_reg_targets
        
         # 由于anchor_target()函数多做了一步分解到level的操作，这里需要重新把
        # 所有anchor合并到(b, sum(wi*hi*6), 4)
        num_images = len(img_metas)
        all_cls_scores = torch.cat([s.permute(0, 2, 3, 1).reshape(
                num_images, -1, self.num_classes) for s in cls_scores], 1)
        all_labels = torch.cat(labels_list, -1).view(num_images, -1)
        all_label_weights = torch.cat(label_weights_list, -1).view(num_images, -1)
        all_bbox_preds = torch.cat([b.permute(0, 2, 3, 1).reshape(
                num_images, -1, 4) for b in bbox_preds], -2)
        all_bbox_targets = torch.cat(bbox_targets_list, -2).view(num_images, -1, 4)
        all_bbox_weights = torch.cat(bbox_weights_list, -2).view(num_images, -1, 4)
        
        # calculate losses
        # TODO: num_total_samples数据？？
        losses_cls, losses_reg = multi_apply(self.loss_single,
                                             all_cls_scores,
                                             all_bbox_preds,
                                             all_labels,
                                             all_label_weights,
                                             all_bbox_targets,
                                             all_bbox_weights,
                                             num_total_samples=num_total_pos,
                                             cfg=cfg)
        return dict(loss_cls=losses_cls, loss_reg=losses_reg)

    def get_bboxes(self, cls_scores, bbox_preds, img_metas, cfg, rescale=False):
        """用于在test时计算bbox"""
        assert len(cls_scores) == len(bbox_preds)
        num_levels = len(cls_scores)

        mlvl_anchors = [
            self.anchor_generators[i].grid_anchors(cls_scores[i].size()[-2:],
                                                   self.anchor_strides[i])
            for i in range(num_levels)
        ]
        result_list = []
        for img_id in range(len(img_metas)):
            cls_score_list = [
                cls_scores[i][img_id].detach() for i in range(num_levels)
            ]
            bbox_pred_list = [
                bbox_preds[i][img_id].detach() for i in range(num_levels)
            ]
            img_shape = img_metas[img_id]['img_shape']
            scale_factor = img_metas[img_id]['scale_factor']
            proposals = self.get_bboxes_single(cls_score_list, bbox_pred_list,
                                               mlvl_anchors, img_shape,
                                               scale_factor, cfg, rescale)
            result_list.append(proposals)
        return result_list

    def get_bboxes_single(self,
                          cls_scores,
                          bbox_preds,
                          mlvl_anchors,
                          img_shape,
                          scale_factor,
                          cfg,
                          rescale=False):
        assert len(cls_scores) == len(bbox_preds) == len(mlvl_anchors)
        mlvl_bboxes = []
        mlvl_scores = []
        for cls_score, bbox_pred, anchors in zip(cls_scores, bbox_preds,
                                                 mlvl_anchors):
            assert cls_score.size()[-2:] == bbox_pred.size()[-2:]
            cls_score = cls_score.permute(1, 2, 0).reshape(
                -1, self.cls_out_channels)
            if self.use_sigmoid_cls:
                scores = cls_score.sigmoid()
            else:
                scores = cls_score.softmax(-1)
            bbox_pred = bbox_pred.permute(1, 2, 0).reshape(-1, 4)
            nms_pre = cfg.get('nms_pre', -1)
            if nms_pre > 0 and scores.shape[0] > nms_pre:
                if self.use_sigmoid_cls:
                    max_scores, _ = scores.max(dim=1)
                else:
                    max_scores, _ = scores[:, 1:].max(dim=1)
                _, topk_inds = max_scores.topk(nms_pre)
                anchors = anchors[topk_inds, :]
                bbox_pred = bbox_pred[topk_inds, :]
                scores = scores[topk_inds, :]
            bboxes = delta2bbox(anchors, bbox_pred, self.target_means,
                                self.target_stds, img_shape)
            mlvl_bboxes.append(bboxes)
            mlvl_scores.append(scores)
        mlvl_bboxes = torch.cat(mlvl_bboxes)
        if rescale:
            mlvl_bboxes /= mlvl_bboxes.new_tensor(scale_factor)
        mlvl_scores = torch.cat(mlvl_scores)
        if self.use_sigmoid_cls:
            padding = mlvl_scores.new_zeros(mlvl_scores.shape[0], 1)
            mlvl_scores = torch.cat([padding, mlvl_scores], dim=1)
        det_bboxes, det_labels = multiclass_nms(
            mlvl_bboxes, mlvl_scores, cfg.score_thr, cfg.nms, cfg.max_per_img)
        return det_bboxes, det_labels


# %%
class SSDHead(nn.Module):

    def __init__(self,
                 input_size=300,
                 num_classes=81,
                 in_channels=(512, 1024, 512, 256, 256, 256),
                 anchor_strides=(8, 16, 32, 64, 100, 300),
                 basesize_ratio_range=(0.1, 0.9),
                 anchor_ratios=([2], [2, 3], [2, 3], [2, 3], [2], [2]),
                 target_means=(.0, .0, .0, .0),
                 target_stds=(1.0, 1.0, 1.0, 1.0),
                 **kwargs): # 增加一个多余变量，避免修改cfg, 里边有一个type变量没有用
        super().__init__()
        
        self.input_size = input_size
        self.num_classes = num_classes
        self.in_channels = in_channels
        self.cls_out_channels = num_classes
        num_anchors = [len(ratios) * 2 + 2 for ratios in anchor_ratios]
        reg_convs = []
        cls_convs = []
        for i in range(len(in_channels)):
            reg_convs.append(
                nn.Conv2d(
                    in_channels[i],
                    num_anchors[i] * 4,
                    kernel_size=3,
                    padding=1))
            cls_convs.append(
                nn.Conv2d(
                    in_channels[i],
                    num_anchors[i] * num_classes,
                    kernel_size=3,
                    padding=1))
        self.reg_convs = nn.ModuleList(reg_convs)
        self.cls_convs = nn.ModuleList(cls_convs)

        min_ratio, max_ratio = basesize_ratio_range
        min_ratio = int(min_ratio * 100)
        max_ratio = int(max_ratio * 100)
        step = int(np.floor(max_ratio - min_ratio) / (len(in_channels) - 2))
        min_sizes = []
        max_sizes = []
        for r in range(int(min_ratio), int(max_ratio) + 1, step):
            min_sizes.append(int(input_size * r / 100))
            max_sizes.append(int(input_size * (r + step) / 100))
        if input_size == 300:
            if basesize_ratio_range[0] == 0.15:  # SSD300 COCO
                min_sizes.insert(0, int(input_size * 7 / 100))
                max_sizes.insert(0, int(input_size * 15 / 100))
            elif basesize_ratio_range[0] == 0.2:  # SSD300 VOC
                min_sizes.insert(0, int(input_size * 10 / 100))
                max_sizes.insert(0, int(input_size * 20 / 100))
        elif input_size == 512:
            if basesize_ratio_range[0] == 0.1:  # SSD512 COCO
                min_sizes.insert(0, int(input_size * 4 / 100))
                max_sizes.insert(0, int(input_size * 10 / 100))
            elif basesize_ratio_range[0] == 0.15:  # SSD512 VOC
                min_sizes.insert(0, int(input_size * 7 / 100))
                max_sizes.insert(0, int(input_size * 15 / 100))
        self.anchor_generators = []
        self.anchor_strides = anchor_strides
        
        for k in range(len(anchor_strides)):
            base_size = min_sizes[k]
            stride = anchor_strides[k]
            ctr = ((stride - 1) / 2., (stride - 1) / 2.)
            scales = [1., np.sqrt(max_sizes[k] / min_sizes[k])]
            ratios = [1.]
            for r in anchor_ratios[k]:
                ratios += [1 / r, r]  # 4 or 6 ratio
            anchor_generator = AnchorGenerator(
                base_size, scales, ratios, scale_major=False, ctr=ctr)
            indices = list(range(len(ratios)))
            indices.insert(1, len(indices))
            anchor_generator.base_anchors = torch.index_select(
                anchor_generator.base_anchors, 0, torch.LongTensor(indices))
            self.anchor_generators.append(anchor_generator)

        self.target_means = target_means
        self.target_stds = target_stds
        self.use_sigmoid_cls = False
        self.use_focal_loss = False

    def init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                xavier_init(m, distribution='uniform', bias=0)

    def forward(self, feats):
        """ssd feats层数不同，不能统一用一种conv，也就不能multi apply同一种forward_single
        这里直接采用循环分别计算每一层的输出。
        """
        cls_scores = []
        bbox_preds = []
        for feat, reg_conv, cls_conv in zip(feats, self.reg_convs,
                                            self.cls_convs):
            cls_scores.append(cls_conv(feat))
            bbox_preds.append(reg_conv(feat))
        return cls_scores, bbox_preds
    
    def get_anchors(self, featmap_sizes, img_metas):
        """Get anchors according to feature map sizes.

        Args:
            featmap_sizes (list[tuple]): Multi-level feature map sizes.
            img_metas (list[dict]): Image meta info.

        Returns:
            tuple: anchors of each image, valid flags of each image
        """
        num_imgs = len(img_metas)
        num_levels = len(featmap_sizes)

        # since feature map sizes of all images are the same, we only compute
        # anchors for one time
        multi_level_anchors = []
        for i in range(num_levels):
            anchors = self.anchor_generators[i].grid_anchors(
                featmap_sizes[i], self.anchor_strides[i])
            multi_level_anchors.append(anchors)
        anchor_list = [multi_level_anchors for _ in range(num_imgs)]

        # for each image, we compute valid flags of multi level anchors
        valid_flag_list = []
        for img_id, img_meta in enumerate(img_metas):
            multi_level_flags = []
            for i in range(num_levels):
                anchor_stride = self.anchor_strides[i]
                feat_h, feat_w = featmap_sizes[i]
                h, w, _ = img_meta['pad_shape']
                valid_feat_h = min(int(np.ceil(h / anchor_stride)), feat_h)
                valid_feat_w = min(int(np.ceil(w / anchor_stride)), feat_w)
                flags = self.anchor_generators[i].valid_flags(
                    (feat_h, feat_w), (valid_feat_h, valid_feat_w))
                multi_level_flags.append(flags)
            valid_flag_list.append(multi_level_flags)

        return anchor_list, valid_flag_list
    
    def loss_single(self, cls_score, bbox_pred, labels, label_weights,
                    bbox_targets, bbox_weights, num_total_samples, cfg):
        loss_cls_all = F.cross_entropy(
            cls_score, labels, reduction='none') * label_weights
        pos_inds = (labels > 0).nonzero().view(-1)
        neg_inds = (labels == 0).nonzero().view(-1)
        
        # hard negtive mining解决样本不平衡问题：提取负样本中损失最大的前k个，确保正负样本比例是1：3
        num_pos_samples = pos_inds.size(0)
        num_neg_samples = cfg.neg_pos_ratio * num_pos_samples
        if num_neg_samples > neg_inds.size(0):
            num_neg_samples = neg_inds.size(0)
        topk_loss_cls_neg, _ = loss_cls_all[neg_inds].topk(num_neg_samples)
        loss_cls_pos = loss_cls_all[pos_inds].sum()
        loss_cls_neg = topk_loss_cls_neg.sum()
        loss_cls = (loss_cls_pos + loss_cls_neg) / num_total_samples

        loss_reg = weighted_smoothl1(
            bbox_pred,
            bbox_targets,
            bbox_weights,
            beta=cfg.smoothl1_beta,
            avg_factor=num_total_samples)
        return loss_cls[None], loss_reg

    def loss(self, cls_scores, bbox_preds, gt_bboxes, gt_labels, img_metas,
             cfg):
        featmap_sizes = [featmap.size()[-2:] for featmap in cls_scores]
        assert len(featmap_sizes) == len(self.anchor_generators)
        # 获得anchor_list
        anchor_list, valid_flag_list = self.get_anchors(
            featmap_sizes, img_metas)
        # 获得anchor
        cls_reg_targets = anchor_target(
            anchor_list,
            valid_flag_list,
            gt_bboxes,
            img_metas,
            self.target_means,
            self.target_stds,
            cfg,
            gt_labels_list=gt_labels,
            label_channels=1,
            sampling=False,
            unmap_outputs=False)
        if cls_reg_targets is None:
            return None
        (labels_list, label_weights_list, bbox_targets_list, bbox_weights_list,
         num_total_pos, num_total_neg) = cls_reg_targets

        num_images = len(img_metas)
        all_cls_scores = torch.cat([
            s.permute(0, 2, 3, 1).reshape(
                num_images, -1, self.cls_out_channels) for s in cls_scores
        ], 1)
        all_labels = torch.cat(labels_list, -1).view(num_images, -1)
        all_label_weights = torch.cat(label_weights_list, -1).view(
            num_images, -1)
        all_bbox_preds = torch.cat([
            b.permute(0, 2, 3, 1).reshape(num_images, -1, 4)
            for b in bbox_preds
        ], -2)
        all_bbox_targets = torch.cat(bbox_targets_list, -2).view(
            num_images, -1, 4)
        all_bbox_weights = torch.cat(bbox_weights_list, -2).view(
            num_images, -1, 4)

        losses_cls, losses_reg = multi_apply(
            self.loss_single,
            all_cls_scores,
            all_bbox_preds,
            all_labels,
            all_label_weights,
            all_bbox_targets,
            all_bbox_weights,
            num_total_samples=num_total_pos,
            cfg=cfg)
        return dict(loss_cls=losses_cls, loss_reg=losses_reg)
    
    def get_bboxes(self, cls_scores, bbox_preds, img_metas, cfg, rescale=False):
        """用于在test时计算bbox"""
        assert len(cls_scores) == len(bbox_preds)
        num_levels = len(cls_scores)

        mlvl_anchors = [
            self.anchor_generators[i].grid_anchors(cls_scores[i].size()[-2:],
                                                   self.anchor_strides[i])
            for i in range(num_levels)
        ]
        result_list = []
        for img_id in range(len(img_metas)):
            cls_score_list = [
                cls_scores[i][img_id].detach() for i in range(num_levels)
            ]
            bbox_pred_list = [
                bbox_preds[i][img_id].detach() for i in range(num_levels)
            ]
            img_shape = img_metas[img_id]['img_shape']
            scale_factor = img_metas[img_id]['scale_factor']
            proposals = self.get_bboxes_single(cls_score_list, bbox_pred_list,
                                               mlvl_anchors, img_shape,
                                               scale_factor, cfg, rescale)
            result_list.append(proposals)
        return result_list

    def get_bboxes_single(self,
                          cls_scores,
                          bbox_preds,
                          mlvl_anchors,
                          img_shape,
                          scale_factor,
                          cfg,
                          rescale=False):
        assert len(cls_scores) == len(bbox_preds) == len(mlvl_anchors)
        mlvl_bboxes = []
        mlvl_scores = []
        for cls_score, bbox_pred, anchors in zip(cls_scores, bbox_preds,
                                                 mlvl_anchors):
            assert cls_score.size()[-2:] == bbox_pred.size()[-2:]
            cls_score = cls_score.permute(1, 2, 0).reshape(
                -1, self.cls_out_channels)
            if self.use_sigmoid_cls:
                scores = cls_score.sigmoid()
            else:
                scores = cls_score.softmax(-1)
            bbox_pred = bbox_pred.permute(1, 2, 0).reshape(-1, 4)
            nms_pre = cfg.get('nms_pre', -1)
            if nms_pre > 0 and scores.shape[0] > nms_pre:
                if self.use_sigmoid_cls:
                    max_scores, _ = scores.max(dim=1)
                else:
                    max_scores, _ = scores[:, 1:].max(dim=1)
                _, topk_inds = max_scores.topk(nms_pre)
                anchors = anchors[topk_inds, :]
                bbox_pred = bbox_pred[topk_inds, :]
                scores = scores[topk_inds, :]
            bboxes = delta2bbox(anchors, bbox_pred, self.target_means,
                                self.target_stds, img_shape)
            mlvl_bboxes.append(bboxes)
            mlvl_scores.append(scores)
        mlvl_bboxes = torch.cat(mlvl_bboxes)
        if rescale:
            mlvl_bboxes /= mlvl_bboxes.new_tensor(scale_factor)
        mlvl_scores = torch.cat(mlvl_scores)
        if self.use_sigmoid_cls:
            padding = mlvl_scores.new_zeros(mlvl_scores.shape[0], 1)
            mlvl_scores = torch.cat([padding, mlvl_scores], dim=1)
        det_bboxes, det_labels = multiclass_nms(
            mlvl_bboxes, mlvl_scores, cfg.score_thr, cfg.nms, cfg.max_per_img)
        return det_bboxes, det_labels
