# ------------------------------------------------------------------------
# modified by fan xiaofeng to generate coco annotations on phocal
# ------------------------------------------------------------------------
# PoET: Pose Estimation Transformer for Single-View, Multi-Object 6D Pose Estimation
# Copyright (c) 2022 Thomas Jantos (thomas.jantos@aau.at), University of Klagenfurt - Control of Networked Systems (CNS). All Rights Reserved.
# Licensed under the BSD-2-Clause-License with no commercial use [see LICENSE for details]
# ------------------------------------------------------------------------
# this script is used to generalize the phocal offical train and test.json
import json
import os
import os.path as osp
import cv2
import numpy as np
import _pickle as cPickle
from utils import data_utils
from tqdm import tqdm


# annotation_set = "test_all" # Choices: train, test
# data_set = "real" #Choices: camera, real
annotation_set = "train" # Choices: train, test, train_all, test_all ,train2test
data_set = 'camera_real' #Choices: camera_real

base_path = '/root/commonfile/fxf/nocs/' #2080
#base_path = '/home1/fanxiaofeng/nocs/' #3090
output_base_path = './'

camera_intrinsics = [577.5, 577.5, 319.5, 239.5] #相机内参矩阵初始化
real_intrinsics = [591.0125, 590.16775, 322.525, 244.11084]


if data_set=="camera_real":
    data_path_c=osp.join(base_path,"CAMERA")
    data_path_r=osp.join(base_path,"Real")
else:
    print("wrong data_set")
    raise TypeError

 # 'bottle', 'bowl', 'camera', 'can', 'laptop', 'mug'
categories = [ #只用超类作为监督信息
    {'supercategory': 'bottle', 'id': 0, 'name': 'bottle'},
    {'supercategory': 'bowl', 'id': 1, 'name': 'bowl'},
    {'supercategory': 'camera', 'id': 2, 'name': 'camera'},
    {'supercategory': 'can', 'id': 3, 'name': 'can'},
    {'supercategory': 'laptop', 'id': 4, 'name': 'laptop'},
    {'supercategory': 'mug', 'id': 5, 'name': 'mug'},
]

annotations = {'images': [],
               'annotations': [],
               'categories': categories}
image_id = 0
annotation_id = 0


if data_set=='camera_real':
    data_dirs_c=np.loadtxt(data_path_c+'/train_list.txt',dtype=str)
    data_dirs_r=np.loadtxt(data_path_r+'/train_list.txt',dtype=str)
    annotation_path = 'crtrain_scale.json'
else:
    print("wrong data_set")
    raise TypeError

fx,fy,cx,cy=camera_intrinsics

K = np.array([
        [fx, 0, cx],
        [0, fy, cy],
        [0,  0,  1]
    ])

intrinsic={}
intrinsic['cx']=cx
intrinsic['cy']=cy
intrinsic['fx']=fx
intrinsic['fy']=fy

for data_dir in tqdm(data_dirs_c):
    
    data_abs_prefix=osp.join(data_path_c,data_dir)
    #print("Annotating",data_abs_prefix)
    file_name=data_abs_prefix+'_color.png'
    label_path=data_abs_prefix+'_label.pkl'
    img_annotation_counter = 0
    if not osp.exists(label_path): #跳过没有标注的部分
        print("No label, skip")
        continue
    with open(label_path,'rb') as f:
        data_annotations=cPickle.load(f)

    if 'size' not in data_annotations:
        print('no size data break')
        break
    for i in range(len(data_annotations['class_ids'])):
        
        if 'handle_visibility' in data_annotations:
            handle_visibility=int(data_annotations['handle_visibility'][i])
            iscrowd=0
        else:
            handle_visibility=1
            iscrowd=0
        bbox_2pts=data_annotations['bboxes'][i].tolist() #y1 x1 y2 x2
        bbox=[bbox_2pts[1], bbox_2pts[0], bbox_2pts[3]-bbox_2pts[1], bbox_2pts[2]-bbox_2pts[0]] # x1 y1 w h
        scale_norm = data_annotations['scales'][i]
        bbox_size=data_annotations['size'][i]*scale_norm
        cat_id=int(data_annotations['class_ids'][i]-1) #delete the background class
        if 0 in bbox_size:
            print("dirty size data skip this annotation")
            continue
        if 'poses' in data_annotations and cat_id!=5: #mug目前没有正确的pose
            pose_RT=data_annotations['poses'][i]
            position=pose_RT[:3,3].reshape(3)
            rotation=pose_RT[:3,:3].reshape(9)
        else:
            sRT = np.identity(4, dtype=np.float32)
            sRT[:3, :3] = data_annotations['scales'][i] * data_annotations['rotations'][i]
            sRT[:3, 3] = data_annotations['translations'][i]
            pose_RT = sRT
            position=pose_RT[:3,3].reshape(3)
            rotation=pose_RT[:3,:3].reshape(9)
        

        rotation_norm = data_annotations['rotations'][i].reshape(9)
        scale_norm = data_annotations['scales'][i]

        obj_annotation = {
                        'id': annotation_id,
                        'image_id': image_id,
                        'relative_pose': {
                            'position': position.tolist(),
                            'rotation': rotation_norm.tolist()
                        },
                        'norm_pose' : {
                            'rotation_norm': rotation_norm.tolist(),
                            'scale_norm' : float(scale_norm)
                        },
                        'bbox': bbox,
                        'bbox_3d_size' : bbox_size.tolist(),
                        'area': int(bbox[2] * bbox[3]),
                        'iscrowd': iscrowd,
                        'handle_visibility': handle_visibility,
                        'category_id': cat_id
                    }

        img_annotation_counter += 1
        annotations['annotations'].append(obj_annotation)
        annotation_id += 1

    if img_annotation_counter == 0:
            print("Image skipped! No annotations valid!")
            continue
    img_annotation = { #获得img_annotation
            'file_name': file_name,
            'id': image_id,
            'width': 640,
            'height': 480,
            'intrinsics': intrinsic,
            'type' : 'camera'
        }
    annotations['images'].append(img_annotation)
    image_id += 1

fx,fy,cx,cy=real_intrinsics

K = np.array([
        [fx, 0, cx],
        [0, fy, cy],
        [0,  0,  1]
    ])

intrinsic={}
intrinsic['cx']=cx
intrinsic['cy']=cy
intrinsic['fx']=fx
intrinsic['fy']=fy

for data_dir in tqdm(data_dirs_r):
    
    data_abs_prefix=osp.join(data_path_r,data_dir)
    #print("Annotating",data_abs_prefix)
    file_name=data_abs_prefix+'_color.png'
    label_path=data_abs_prefix+'_label.pkl'
    img_annotation_counter = 0
    if not osp.exists(label_path): #跳过没有标注的部分
        print("No label, skip")
        continue
    with open(label_path,'rb') as f:
        data_annotations=cPickle.load(f)

    if 'size' not in data_annotations:
        print('no size data break')
        break
    for i in range(len(data_annotations['class_ids'])):
        
        if 'handle_visibility' in data_annotations:
            handle_visibility=int(data_annotations['handle_visibility'][i])
            iscrowd=0
        else:
            handle_visibility=1
            iscrowd=0
        bbox_2pts=data_annotations['bboxes'][i].tolist() #y1 x1 y2 x2
        bbox=[bbox_2pts[1], bbox_2pts[0], bbox_2pts[3]-bbox_2pts[1], bbox_2pts[2]-bbox_2pts[0]] # x1 y1 w h
        scale_norm = data_annotations['scales'][i]
        bbox_size=data_annotations['size'][i]*scale_norm
        cat_id=int(data_annotations['class_ids'][i]-1) #delete the background class
        if 0 in bbox_size:
            print("dirty size data skip this annotation")
            print("class label is ",cat_id)
            continue
        if 'poses' in data_annotations: #mug目前没有正确的pose
            pose_RT=data_annotations['poses'][i]
            position=pose_RT[:3,3].reshape(3)
            rotation=pose_RT[:3,:3].reshape(9)
        else:
            sRT = np.identity(4, dtype=np.float32)
            sRT[:3, :3] = data_annotations['scales'][i] * data_annotations['rotations'][i]
            sRT[:3, 3] = data_annotations['translations'][i]
            pose_RT = sRT
            position=pose_RT[:3,3].reshape(3)
            rotation=pose_RT[:3,:3].reshape(9)
        
        rotation_norm = data_annotations['rotations'][i].reshape(9)

        obj_annotation = {
                        'id': annotation_id,
                        'image_id': image_id,
                        'relative_pose': {
                            'position': position.tolist(),
                            'rotation': rotation_norm.tolist()
                        },
                        'norm_pose' : {
                            'rotation_norm': rotation_norm.tolist(),
                            'scale_norm' : float(scale_norm)
                        },
                        'bbox': bbox,
                        'bbox_3d_size' : bbox_size.tolist(),
                        'area': int(bbox[2] * bbox[3]),
                        'iscrowd': iscrowd,
                        'handle_visibility': handle_visibility,
                        'category_id': cat_id
                    }
        img_annotation_counter += 1
        annotations['annotations'].append(obj_annotation)
        annotation_id += 1

    if img_annotation_counter == 0:
            print("Image skipped! No annotations valid!")
            continue
    img_annotation = { #获得img_annotation
            'file_name': file_name,
            'id': image_id,
            'width': 640,
            'height': 480,
            'intrinsics': intrinsic,
            'type' : 'real',
        }
    annotations['images'].append(img_annotation)
    image_id += 1


#print(annotations)
with open(output_base_path + annotation_path, 'w') as out_file:
    json.dump(annotations, out_file , indent=4)





