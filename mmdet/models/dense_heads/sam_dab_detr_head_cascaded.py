# Copyright (c) OpenMMLab. All rights reserved.
__author__= 'fanxiaofeng'
from typing import Dict, List, Tuple
from mmengine.structures import InstanceData
from ..utils import multi_apply

import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.cnn import Linear
from mmengine.model import bias_init_with_prob, constant_init
from torch import Tensor

from mmdet.registry import MODELS,TASK_UTILS
from mmdet.structures import SampleList
from mmdet.structures.bbox import bbox_cxcywh_to_xyxy, bbox_xyxy_to_cxcywh
from mmdet.utils import InstanceList
from ..layers import MLP, inverse_sigmoid
from .conditional_detr_head import ConditionalDETRHead
from mmdet.utils import (ConfigType, InstanceList, OptInstanceList,
                         OptMultiConfig, reduce_mean)


@MODELS.register_module()
class SAMDABDETRHeadCascaded(ConditionalDETRHead):
    """Head of DAB-DETR. DAB-DETR: Dynamic Anchor Boxes are Better Queries for
    DETR.

    More details can be found in the `paper
    <https://arxiv.org/abs/2201.12329>`_ .
    """

    def __init__(
            self,
            loss_R:ConfigType = dict(type='SmoothL1Loss', loss_weight=1.0),
            loss_T:ConfigType = dict(type='MSELoss', loss_weight=1.0),
            loss_size:ConfigType = dict(type='MSELoss', loss_weight=1.0),
            **kwargs) -> None:
        super(SAMDABDETRHeadCascaded,self).__init__(**kwargs)
        self.loss_R = MODELS.build(loss_R)
        self.loss_T = MODELS.build(loss_T)
        self.loss_size = MODELS.build(loss_size)
        

        

    def _init_layers(self) -> None:
        """Initialize layers of the transformer head."""
        # cls branch
        self.fc_cls = Linear(self.embed_dims, self.cls_out_channels)
        # rot branch
        self.fc_reg_R = MLP(self.embed_dims, self.embed_dims, 9, 3) #先输出6维结果 再转成9维矩阵
        # trs branch
        self.fc_reg_T = MLP(self.embed_dims*2, self.embed_dims, 3, 4) #3X1 1 Mat
        # reg branch output hidden states
        self.fc_reg_first = MLP(self.embed_dims, self.embed_dims, self.embed_dims, 3) #先输出回归的最后一层用于translation和size的预测
        # reg branch
        self.fc_reg = MLP(self.embed_dims, self.embed_dims, 4, 1)
        # size branch
        self.fc_reg_size = MLP(self.embed_dims*2, self.embed_dims, 3, 4) #预测3d bbox的size

    def init_weights(self) -> None:
        """initialize weights."""
        if self.loss_cls.use_sigmoid:
            bias_init = bias_init_with_prob(0.01)
            nn.init.constant_(self.fc_cls.bias, bias_init)
        constant_init(self.fc_reg_first.layers[-1], 0., bias=0.)
        constant_init(self.fc_reg.layers[-1], 0., bias=0.)
        constant_init(self.fc_reg_R.layers[-1], 0., bias=0.)
        constant_init(self.fc_reg_T.layers[-1], 0., bias=0.)
        constant_init(self.fc_reg_size.layers[-1], 0., bias=0.)

    def forward(self, hidden_states: Tensor,
                references: Tensor) -> Tuple[Tensor, Tensor ,Tensor,Tensor,Tensor]:
        """"Forward function.

        Args:
            hidden_states (Tensor): Features from transformer decoder. If
                `return_intermediate_dec` is True output has shape
                (num_decoder_layers, bs, num_queries, dim), else has shape (1,
                bs, num_queries, dim) which only contains the last layer
                outputs.
            references (Tensor): References from transformer decoder. If
                `return_intermediate_dec` is True output has shape
                (num_decoder_layers, bs, num_queries, 2/4), else has shape (1,
                bs, num_queries, 2/4)
                which only contains the last layer reference.
        Returns:
            tuple[Tensor]: results of head containing the following tensor.

            - layers_cls_scores (Tensor): Outputs from the classification head,
              shape (num_decoder_layers, bs, num_queries, cls_out_channels).
              Note cls_out_channels should include background.
            - layers_bbox_preds (Tensor): Sigmoid outputs from the regression
              head with normalized coordinate format (cx, cy, w, h), has shape
              (num_decoder_layers, bs, num_queries, 4).
            - layers_bbox_preds_R  layers_bbox_preds_T
        """
        layers_cls_scores = self.fc_cls(hidden_states)
        layers_bbox_preds_R = self.fc_reg_R(hidden_states)
        #layers_bbox_preds_R = self.rotation_6d_to_matrix(layers_bbox_preds_R_6d) #6d 到9d
        references_before_sigmoid = inverse_sigmoid(references, eps=1e-3)
        bbox_hidden_states=self.fc_reg_first(hidden_states)
        tmp_reg_preds = self.fc_reg(bbox_hidden_states)
        tmp_reg_preds[..., :references_before_sigmoid.
                      size(-1)] += references_before_sigmoid
        layers_bbox_preds = tmp_reg_preds.sigmoid()
        hidden_states_bbox=torch.cat([hidden_states,bbox_hidden_states],dim=-1)
        layers_bbox_preds_T = self.fc_reg_T(hidden_states_bbox)
        layers_bbox_preds_size = self.fc_reg_size(hidden_states_bbox)
        return layers_cls_scores,layers_bbox_preds,layers_bbox_preds_R,layers_bbox_preds_T,layers_bbox_preds_size

    def rotation_6d_to_matrix(self, rot_6d): #from PoET
        """
        Given a 6D rotation output, calculate the 3D rotation matrix in SO(3) using the Gramm Schmit process

        For details: https://openaccess.thecvf.com/content_CVPR_2019/papers/Zhou_On_the_Continuity_of_Rotation_Representations_in_Neural_Networks_CVPR_2019_paper.pdf
        """
        num_layers, bs , n_q, _ = rot_6d.shape
        rot_6d = rot_6d.view(-1, 6)
        m1 = rot_6d[:, 0:3]
        m2 = rot_6d[:, 3:6]

        x = F.normalize(m1, p=2, dim=1)
        z = torch.cross(x, m2, dim=1)
        z = F.normalize(z, p=2, dim=1)
        y = torch.cross(z, x, dim=1)
        rot_matrix = torch.cat((x.view(-1, 3, 1), y.view(-1, 3, 1), z.view(-1, 3, 1)), 2)  # Rotation Matrix lying in the SO(3)
        rot_matrix = rot_matrix.view(num_layers,bs, n_q, 9)  #.transpose(2, 3)
        return rot_matrix

    def predict(self,
                hidden_states: Tensor,
                references: Tensor,
                batch_data_samples: SampleList,
                rescale: bool = True) -> InstanceList:
        """Perform forward propagation of the detection head and predict
        detection results on the features of the upstream network. Over-write
        because img_metas are needed as inputs for bbox_head.

        Args:
            hidden_states (Tensor): Feature from the transformer decoder, has
                shape (num_decoder_layers, bs, num_queries, dim).
            references (Tensor): references from the transformer decoder, has
                shape (num_decoder_layers, bs, num_queries, 2/4).
            batch_data_samples (List[:obj:`DetDataSample`]): The Data
                Samples. It usually includes information such as
                `gt_instance`, `gt_panoptic_seg` and `gt_sem_seg`.
            rescale (bool, optional): Whether to rescale the results.
                Defaults to True.

        Returns:
            list[obj:`InstanceData`]: Detection results of each image
            after the post process.
        """
        batch_img_metas = [
            data_samples.metainfo for data_samples in batch_data_samples
        ]

        last_layer_hidden_state = hidden_states[-1].unsqueeze(0)
        last_layer_reference = references[-1].unsqueeze(0)
        outs = self(last_layer_hidden_state, last_layer_reference)

        predictions = self.predict_by_feat(
            *outs, batch_img_metas=batch_img_metas, rescale=rescale)
        return predictions

    def loss_by_feat( #以下内容来自DETRHead
        self,
        all_layers_cls_scores: Tensor,
        all_layers_bbox_preds: Tensor,
        all_layers_bbox_preds_R: Tensor,
        all_layers_bbox_preds_T: Tensor,
        all_layers_bbox_preds_size: Tensor,
        batch_gt_instances: InstanceList,
        batch_img_metas: List[dict],
        batch_gt_instances_ignore: OptInstanceList = None
    ) -> Dict[str, Tensor]:
        """"Loss function.

        Only outputs from the last feature level are used for computing
        losses by default.

        Args:
            all_layers_cls_scores (Tensor): Classification outputs
                of each decoder layers. Each is a 4D-tensor, has shape
                (num_decoder_layers, bs, num_queries, cls_out_channels).
            all_layers_bbox_preds (Tensor): Sigmoid regression
                outputs of each decoder layers. Each is a 4D-tensor with
                normalized coordinate format (cx, cy, w, h) and shape
                (num_decoder_layers, bs, num_queries, 4).
            batch_gt_instances (list[:obj:`InstanceData`]): Batch of
                gt_instance. It usually includes ``bboxes`` and ``labels``
                attributes.
            batch_img_metas (list[dict]): Meta information of each image, e.g.,
                image size, scaling factor, etc.
            batch_gt_instances_ignore (list[:obj:`InstanceData`], optional):
                Batch of gt_instances_ignore. It includes ``bboxes`` attribute
                data that is ignored during training and testing.
                Defaults to None.

        Returns:
            dict[str, Tensor]: A dictionary of loss components.
        """
        assert batch_gt_instances_ignore is None, \
            f'{self.__class__.__name__} only supports ' \
            'for batch_gt_instances_ignore setting to None.'

        losses_cls, losses_bbox, losses_iou, losses_R, losses_T, losses_size = multi_apply(
            self.loss_by_feat_single,
            all_layers_cls_scores,
            all_layers_bbox_preds,
            all_layers_bbox_preds_R,
            all_layers_bbox_preds_T,
            all_layers_bbox_preds_size,
            batch_gt_instances=batch_gt_instances,
            batch_img_metas=batch_img_metas)

        loss_dict = dict()
        # loss from the last decoder layer
        loss_dict['loss_cls'] = losses_cls[-1]
        loss_dict['loss_bbox'] = losses_bbox[-1]
        loss_dict['loss_iou'] = losses_iou[-1]
        loss_dict['loss_R'] = losses_R[-1]
        loss_dict['loss_T'] = losses_T[-1]
        loss_dict['loss_size'] = losses_size[-1]
        # loss from other decoder layers
        num_dec_layer = 0
        for loss_cls_i,loss_bbox_i,loss_iou_i, loss_R_i, loss_T_i, loss_size_i in \
                zip(losses_cls[:-1], losses_bbox[:-1], losses_iou[:-1], losses_R[:-1], losses_T[:-1], losses_size[:-1]):
            loss_dict[f'd{num_dec_layer}.loss_cls'] = loss_cls_i
            loss_dict[f'd{num_dec_layer}.loss_bbox'] = loss_bbox_i
            loss_dict[f'd{num_dec_layer}.loss_iou'] = loss_iou_i
            loss_dict[f'd{num_dec_layer}.loss_R'] = loss_R_i
            loss_dict[f'd{num_dec_layer}.loss_T'] = loss_T_i
            loss_dict[f'd{num_dec_layer}.loss_size'] = loss_size_i
            num_dec_layer += 1
        return loss_dict

    def loss_by_feat_single(self, cls_scores: Tensor, bbox_preds: Tensor, bbox_R_preds: Tensor, bbox_T_preds: Tensor,bbox_size_preds: Tensor,
                            batch_gt_instances: InstanceList,
                            batch_img_metas: List[dict]) -> Tuple[Tensor]:
        """Loss function for outputs from a single decoder layer of a single
        feature level.

        Args:
            cls_scores (Tensor): Box score logits from a single decoder layer
                for all images, has shape (bs, num_queries, cls_out_channels).
            bbox_preds (Tensor): Sigmoid outputs from a single decoder layer
                for all images, with normalized coordinate (cx, cy, w, h) and
                shape (bs, num_queries, 4).
            batch_gt_instances (list[:obj:`InstanceData`]): Batch of
                gt_instance. It usually includes ``bboxes`` and ``labels``
                attributes.
            batch_img_metas (list[dict]): Meta information of each image, e.g.,
                image size, scaling factor, etc.

        Returns:
            Tuple[Tensor]: A tuple including `loss_cls`, `loss_box` and
            `loss_iou`.
        """
        num_imgs = cls_scores.size(0)
        cls_scores_list = [cls_scores[i] for i in range(num_imgs)]
        bbox_preds_list = [bbox_preds[i] for i in range(num_imgs)]
        bbox_R_preds_list = [bbox_R_preds[i] for i in range(num_imgs)]
        bbox_T_preds_list = [bbox_T_preds[i] for i in range(num_imgs)]
        bbox_size_preds_list = [bbox_size_preds[i] for i in range(num_imgs)]
        cls_reg_targets = self.get_targets(cls_scores_list,bbox_preds_list, bbox_R_preds_list, bbox_T_preds_list,bbox_size_preds_list,
                                           batch_gt_instances, batch_img_metas)
        (labels_list, label_weights_list, bbox_targets_list, bbox_weights_list, 
        bbox_R_targets_list, bbox_R_weights_list,
        bbox_T_targets_list, bbox_T_weights_list,
        bbox_size_targets_list, bbox_size_weights_list,
         num_total_pos, num_total_neg) = cls_reg_targets
        labels = torch.cat(labels_list, 0)
        label_weights = torch.cat(label_weights_list, 0)
        bbox_targets = torch.cat(bbox_targets_list, 0)
        bbox_weights = torch.cat(bbox_weights_list, 0)
        bbox_R_targets = torch.cat(bbox_R_targets_list, 0)
        bbox_R_weights = torch.cat(bbox_R_weights_list, 0)
        bbox_T_targets = torch.cat(bbox_T_targets_list, 0)
        bbox_T_weights = torch.cat(bbox_T_weights_list, 0)
        bbox_size_targets = torch.cat(bbox_size_targets_list, 0)
        bbox_size_weights = torch.cat(bbox_size_weights_list, 0)

        # classification loss
        cls_scores = cls_scores.reshape(-1, self.cls_out_channels)
        # construct weighted avg_factor to match with the official DETR repo
        cls_avg_factor = num_total_pos * 1.0 + \
            num_total_neg * self.bg_cls_weight
        if self.sync_cls_avg_factor:
            cls_avg_factor = reduce_mean(
                cls_scores.new_tensor([cls_avg_factor]))
        cls_avg_factor = max(cls_avg_factor, 1)

        loss_cls = self.loss_cls(
            cls_scores, labels, label_weights, avg_factor=cls_avg_factor)

        # Compute the average number of gt boxes across all gpus, for
        # normalization purposes
        num_total_pos = loss_cls.new_tensor([num_total_pos])
        num_total_pos = torch.clamp(reduce_mean(num_total_pos), min=1).item()

        # construct factors used for rescale bboxes
        factors = []
        for img_meta, bbox_pred in zip(batch_img_metas, bbox_preds):
            img_h, img_w, = img_meta['img_shape']
            factor = bbox_pred.new_tensor([img_w, img_h, img_w,
                                           img_h]).unsqueeze(0).repeat(
                                               bbox_pred.size(0), 1)
            factors.append(factor)
        factors = torch.cat(factors, 0)

        # DETR regress the relative position of boxes (cxcywh) in the image,
        # thus the learning target is normalized by the image size. So here
        # we need to re-scale them for calculating IoU loss
        bbox_preds = bbox_preds.reshape(-1, 4)
        bboxes = bbox_cxcywh_to_xyxy(bbox_preds) * factors
        bboxes_gt = bbox_cxcywh_to_xyxy(bbox_targets) * factors

        bbox_R_preds = bbox_R_preds.reshape(-1,9) #这一步必须要做 不然pred和target对不齐
        bbox_T_preds = bbox_T_preds.reshape(-1,3)
        bbox_size_preds = bbox_size_preds.reshape(-1,3)

        # regression IoU loss, defaultly GIoU loss
        loss_iou = self.loss_iou(
            bboxes, bboxes_gt, bbox_weights, avg_factor=num_total_pos)

        # regression L1 loss
        loss_bbox = self.loss_bbox(
            bbox_preds, bbox_targets, bbox_weights, avg_factor=num_total_pos)

        loss_R = self.loss_R(
            bbox_R_preds,  bbox_R_targets,bbox_R_weights,avg_factor=num_total_pos
        )

        loss_T = self.loss_T(
            bbox_T_preds,  bbox_T_targets,bbox_T_weights,avg_factor=num_total_pos
        )

        loss_size = self.loss_size(
            bbox_size_preds,  bbox_size_targets,bbox_size_weights,avg_factor=num_total_pos
        )

        return loss_cls,loss_bbox, loss_iou, loss_R, loss_T, loss_size

    def get_targets(self, cls_scores_list: List[Tensor],
                    bbox_preds_list: List[Tensor],
                    bbox_preds_list_R: List[Tensor],
                    bbox_preds_list_T: List[Tensor],
                    bbox_preds_list_size: List[Tensor],
                    batch_gt_instances: InstanceList,
                    batch_img_metas: List[dict]) -> tuple:
        """Compute regression and classification targets for a batch image.

        Outputs from a single decoder layer of a single feature level are used.

        Args:
            cls_scores_list (list[Tensor]): Box score logits from a single
                decoder layer for each image, has shape [num_queries,
                cls_out_channels].
            bbox_preds_list (list[Tensor]): Sigmoid outputs from a single
                decoder layer for each image, with normalized coordinate
                (cx, cy, w, h) and shape [num_queries, 4].
            batch_gt_instances (list[:obj:`InstanceData`]): Batch of
                gt_instance. It usually includes ``bboxes`` and ``labels``
                attributes.
            batch_img_metas (list[dict]): Meta information of each image, e.g.,
                image size, scaling factor, etc.

        Returns:
            tuple: a tuple containing the following targets.

            - labels_list (list[Tensor]): Labels for all images.
            - label_weights_list (list[Tensor]): Label weights for all images.
            - bbox_targets_list (list[Tensor]): BBox targets for all images.
            - bbox_weights_list (list[Tensor]): BBox weights for all images.
            - num_total_pos (int): Number of positive samples in all images.
            - num_total_neg (int): Number of negative samples in all images.
        """
        (labels_list, label_weights_list, bbox_targets_list, bbox_weights_list,
         bbox_R_targets_list, bbox_R_weights_list,
         bbox_T_targets_list, bbox_T_weights_list,
         bbox_size_targets_list, bbox_size_weights_list,
         pos_inds_list,
         neg_inds_list) = multi_apply(self._get_targets_single,
                                      cls_scores_list,bbox_preds_list, bbox_preds_list_R, bbox_preds_list_T, bbox_preds_list_size,
                                      batch_gt_instances, batch_img_metas)
        num_total_pos = sum((inds.numel() for inds in pos_inds_list))
        num_total_neg = sum((inds.numel() for inds in neg_inds_list))
        return (labels_list, label_weights_list,bbox_targets_list,bbox_weights_list,  bbox_R_targets_list, bbox_R_weights_list, 
                bbox_T_targets_list, bbox_T_weights_list, bbox_size_targets_list, bbox_size_weights_list, num_total_pos, num_total_neg)

    def _get_targets_single(self, cls_score: Tensor,bbox_pred: Tensor, rot_pred: Tensor, pos_pred: Tensor, size_pred: Tensor,
                            gt_instances: InstanceData,
                            img_meta: dict) -> tuple:
        """Compute regression and classification targets for one image.

        Outputs from a single decoder layer of a single feature level are used.

        Args:
            cls_score (Tensor): Box score logits from a single decoder layer
                for one image. Shape [num_queries, cls_out_channels].
            bbox_pred (Tensor): Sigmoid outputs from a single decoder layer
                for one image, with normalized coordinate (cx, cy, w, h) and
                shape [num_queries, 4].
            gt_instances (:obj:`InstanceData`): Ground truth of instance
                annotations. It should includes ``bboxes`` and ``labels``
                attributes.
            img_meta (dict): Meta information for one image.

        Returns:
            tuple[Tensor]: a tuple containing the following for one image.

            - labels (Tensor): Labels of each image.
            - label_weights (Tensor]): Label weights of each image.
            - bbox_targets (Tensor): BBox targets of each image.
            - bbox_weights (Tensor): BBox weights of each image.
            - pos_inds (Tensor): Sampled positive indices for each image.
            - neg_inds (Tensor): Sampled negative indices for each image.
        """
        img_h, img_w = img_meta['img_shape']
        factor = bbox_pred.new_tensor([img_w, img_h, img_w,
                                       img_h]).unsqueeze(0)
        num_bboxes = bbox_pred.size(0)
        # convert bbox_pred from xywh, normalized to xyxy, unnormalized
        bbox_pred = bbox_cxcywh_to_xyxy(bbox_pred)
        bbox_pred = bbox_pred * factor
        rot_pred = rot_pred #这里或许需要一个6d转9*1的操作

        pos_pred = pos_pred
        size_pred = size_pred


        pred_instances = InstanceData(scores=cls_score, bboxes=bbox_pred, rots=rot_pred , poses=pos_pred ,sizes=size_pred)
        # assigner and sampler
        assign_result = self.assigner.assign(
            pred_instances=pred_instances,
            gt_instances=gt_instances,
            img_meta=img_meta)

        gt_bboxes = gt_instances.bboxes
        gt_labels = gt_instances.labels
        gt_rots = gt_instances.rots
        gt_poses = gt_instances.poses
        gt_sizes = gt_instances.sizes
        pos_inds = torch.nonzero(
            assign_result.gt_inds > 0, as_tuple=False).squeeze(-1).unique()
        neg_inds = torch.nonzero(
            assign_result.gt_inds == 0, as_tuple=False).squeeze(-1).unique()
        pos_assigned_gt_inds = assign_result.gt_inds[pos_inds] - 1
        pos_gt_bboxes = gt_bboxes[pos_assigned_gt_inds.long(), :]
        pos_gt_rots = gt_rots[pos_assigned_gt_inds.long(), :]
        pos_gt_poses = gt_poses[pos_assigned_gt_inds.long(), :]
        pos_gt_sizes = gt_sizes[pos_assigned_gt_inds.long(), :]

        # label targets
        labels = gt_bboxes.new_full((num_bboxes, ),
                                    self.num_classes,
                                    dtype=torch.long)
        labels[pos_inds] = gt_labels[pos_assigned_gt_inds]
        label_weights = gt_bboxes.new_ones(num_bboxes)

        # bbox targets
        bbox_targets = torch.zeros_like(bbox_pred)
        bbox_weights = torch.zeros_like(bbox_pred)
        bbox_weights[pos_inds] = 1.0

        # bbox R targets
        bbox_R_targets = torch.zeros_like(rot_pred)
        bbox_R_weights = torch.zeros_like(rot_pred)
        bbox_R_weights[pos_inds] = 1.0

        # bbox T targets
        bbox_T_targets = torch.zeros_like(pos_pred)
        bbox_T_weights = torch.zeros_like(pos_pred)
        bbox_T_weights[pos_inds] = 1.0

        # bbox size targets
        bbox_size_targets = torch.zeros_like(size_pred)
        bbox_size_weights = torch.zeros_like(size_pred)
        bbox_size_weights[pos_inds] = 1.0

        # DETR regress the relative position of boxes (cxcywh) in the image.
        # Thus the learning target should be normalized by the image size, also
        # the box format should be converted from defaultly x1y1x2y2 to cxcywh.
        pos_gt_bboxes_normalized = pos_gt_bboxes / factor
        pos_gt_bboxes_targets = bbox_xyxy_to_cxcywh(pos_gt_bboxes_normalized)
        bbox_targets[pos_inds] = pos_gt_bboxes_targets
        bbox_R_targets[pos_inds] = pos_gt_rots
        bbox_T_targets[pos_inds] = pos_gt_poses
        bbox_size_targets[pos_inds] = pos_gt_sizes
        return (labels, label_weights, bbox_targets, bbox_weights,
                bbox_R_targets, bbox_R_weights, 
                bbox_T_targets, bbox_T_weights,
                bbox_size_targets, bbox_size_weights,
                pos_inds,neg_inds)


    def predict_by_feat(self,
                        layer_cls_scores: Tensor,
                        layer_bbox_preds: Tensor,
                        layer_bbox_R_preds: Tensor,
                        layer_bbox_T_preds: Tensor,
                        layer_bbox_size_preds: Tensor,
                        batch_img_metas: List[dict],
                        rescale: bool = True) -> InstanceList:
        """Transform network outputs for a batch into bbox predictions.

        Args:
            layer_cls_scores (Tensor): Classification outputs of the last or
                all decoder layer. Each is a 4D-tensor, has shape
                (num_decoder_layers, bs, num_queries, cls_out_channels).
            layer_bbox_preds (Tensor): Sigmoid regression outputs of the last
                or all decoder layer. Each is a 4D-tensor with normalized
                coordinate format (cx, cy, w, h) and shape
                (num_decoder_layers, bs, num_queries, 4).
            batch_img_metas (list[dict]): Meta information of each image.
            rescale (bool, optional): If `True`, return boxes in original
                image space. Defaults to `True`.

        Returns:
            list[:obj:`InstanceData`]: Object detection results of each image
            after the post process. Each item usually contains following keys.

                - scores (Tensor): Classification scores, has a shape
                  (num_instance, )
                - labels (Tensor): Labels of bboxes, has a shape
                  (num_instances, ).
                - bboxes (Tensor): Has a shape (num_instances, 4),
                  the last dimension 4 arrange as (x1, y1, x2, y2).
        """
        # NOTE only using outputs from the last feature level,
        # and only the outputs from the last decoder layer is used.
        cls_scores = layer_cls_scores[-1]
        bbox_preds = layer_bbox_preds[-1]
        bbox_preds_R = layer_bbox_R_preds[-1]
        bbox_preds_T = layer_bbox_T_preds[-1]
        bbox_preds_size = layer_bbox_size_preds[-1]

        result_list = []
        for img_id in range(len(batch_img_metas)):
            cls_score = cls_scores[img_id]
            bbox_pred = bbox_preds[img_id]
            bbox_pred_R = bbox_preds_R[img_id]
            bbox_pred_T = bbox_preds_T[img_id]
            bbox_pred_size = bbox_preds_size[img_id]
            img_meta = batch_img_metas[img_id]
            results = self._predict_by_feat_single(cls_score,bbox_pred, bbox_pred_R, bbox_pred_T, bbox_pred_size,
                                                   img_meta, rescale)
            result_list.append(results)
        return result_list

    def _predict_by_feat_single(self,
                                cls_score: Tensor,
                                bbox_pred: Tensor,
                                bbox_pred_R: Tensor,
                                bbox_pred_T: Tensor,
                                bbox_pred_size: Tensor,
                                img_meta: dict,
                                rescale: bool = True) -> InstanceData:
        """Transform outputs from the last decoder layer into bbox predictions
        for each image.

        Args:
            cls_score (Tensor): Box score logits from the last decoder layer
                for each image. Shape [num_queries, cls_out_channels].
            bbox_pred (Tensor): Sigmoid outputs from the last decoder layer
                for each image, with coordinate format (cx, cy, w, h) and
                shape [num_queries, 4].
            img_meta (dict): Image meta info.
            rescale (bool): If True, return boxes in original image
                space. Default True.

        Returns:
            :obj:`InstanceData`: Detection results of each image
            after the post process.
            Each item usually contains following keys.

                - scores (Tensor): Classification scores, has a shape
                  (num_instance, )
                - labels (Tensor): Labels of bboxes, has a shape
                  (num_instances, ).
                - bboxes (Tensor): Has a shape (num_instances, 4),
                  the last dimension 4 arrange as (x1, y1, x2, y2).
        """
        assert len(cls_score) == len(bbox_pred_R)  # num_queries
        max_per_img = self.test_cfg.get('max_per_img', len(cls_score))
        img_shape = img_meta['img_shape']
        # exclude background
        if self.loss_cls.use_sigmoid:
            cls_score = cls_score.sigmoid()
            scores, indexes = cls_score.view(-1).topk(max_per_img)
            det_labels = indexes % self.num_classes
            bbox_index = indexes // self.num_classes
            bbox_pred = bbox_pred[bbox_index]
            bbox_pred_R = bbox_pred_R[bbox_index]
            bbox_pred_T = bbox_pred_T[bbox_index]
            bbox_pred_size = bbox_pred_size[bbox_index]
        else:
            scores, det_labels = F.softmax(cls_score, dim=-1)[..., :-1].max(-1)
            scores, bbox_index = scores.topk(max_per_img)
            bbox_pred = bbox_pred[bbox_index]
            bbox_pred_R = bbox_pred_R[bbox_index]
            bbox_pred_T = bbox_pred_T[bbox_index]
            bbox_pred_size = bbox_pred_size[bbox_index]
            det_labels = det_labels[bbox_index]

        det_bboxes = bbox_cxcywh_to_xyxy(bbox_pred)
        det_bboxes[:, 0::2] = det_bboxes[:, 0::2] * img_shape[1]
        det_bboxes[:, 1::2] = det_bboxes[:, 1::2] * img_shape[0]
        det_bboxes[:, 0::2].clamp_(min=0, max=img_shape[1])
        det_bboxes[:, 1::2].clamp_(min=0, max=img_shape[0])
        if rescale:
            assert img_meta.get('scale_factor') is not None
            det_bboxes /= det_bboxes.new_tensor(
                img_meta['scale_factor']).repeat((1, 2))
        det_R = bbox_pred_R
        det_T = bbox_pred_T
        det_size = bbox_pred_size

        results = InstanceData()
        # results.bboxes = det_bboxes
        results.rots = det_R
        results.poses = det_T
        results.bboxes = det_bboxes
        results.scores = scores
        results.labels = det_labels
        results.sizes = det_size
        return results