# to import files from parent dir
import sys
import os

import torch
from mvtorch.data import ShapeNetPart, CustomDataLoader
from mvtorch.view_selector import MVTN, MVLiftingModule
from mvtorch.mvrenderer import MVRenderer
from mvtorch.networks import MLPClassifier, MVNetwork
from mvtorch.ops import svctomvc

# config variables
nb_views = 1 # Number of views generated by view selector
nb_epochs = 100 # Number of epochs
parallel_head = True # Do segmentation with parallel heads where each head is focused on one class
lambda_l2d = 1 # The 2D CE loss coefficient in the segmentation pipeline
lambda_l3d = 0 # The 3D CE loss coefficient on the segmentation pipeline

# Create dataset and dataloader
dset_train = ShapeNetPart(root_dir='../data/hdf5_data', split='trainval')
dset_test = ShapeNetPart(root_dir='../data/hdf5_data', split='test')
train_loader = CustomDataLoader(dset_train, batch_size=5, shuffle=True, drop_last=True)
test_loader = CustomDataLoader(dset_test, batch_size=5, shuffle=False, drop_last=False)

# Create backbone multi-view network (DeepLabV3 with ResNet-101)
num_classes = len(dset_train.cat2id)
num_parts = max(dset_train.seg_num)
mvnetwork = MVNetwork(num_classes=num_classes, num_parts=num_parts, mode='part', net_name='deeplab').cuda()

# Create backbone optimizer
optimizer = torch.optim.AdamW(mvnetwork.parameters(), lr=0.00001, weight_decay=0.03)

# Create view selector
mvtn = MVTN(nb_views=nb_views).cuda()

# Create optimizer for view selector (In case views are not fixed, otherwise set to None)
# mvtn_optimizer = torch.optim.AdamW(mvtn.parameters(), lr=0.0001, weight_decay=0.01)
mvtn_optimizer = None

# Create multi-view renderer
mvrenderer = MVRenderer(nb_views=nb_views, return_mapping=True)

# Create the MLP classifier
mlp_classifier = MLPClassifier(num_classes=num_classes, num_parts=num_parts)

# Create the multi-view lifting module
mvlifting = MVLiftingModule(image_size=224, lifting_method='mode', mlp_classifier=mlp_classifier, balanced_object_loss=True, balanced_3d_loss_alpha=0, lifting_net=None, use_early_voint_feats=False).cuda()

# Create loss function for training
criterion = torch.nn.CrossEntropyLoss()

# Define function to calculate 2D and 3D loss as well as the 3D predictions
def calc_loss_and_3d_pred(rendered_images, cls, labels_2d, azim, elev, points, indxs, parts_nb, view_info, seg):
    criterion = torch.nn.CrossEntropyLoss(ignore_index=0)
    outputs, feats = mvnetwork(rendered_images, cls)
    loss2d = mvnetwork.get_loss(criterion, outputs, labels_2d, cls)
    views_weights = mvlifting.compute_views_weights(azim, elev, rendered_images, normals=None) 
    predictions_3d = mvlifting.lift_2D_to_3D(points, predictions_2d=svctomvc(outputs, nb_views=nb_views), rendered_pix_to_point=indxs, views_weights=views_weights, cls=cls, parts_nb=parts_nb, view_info=view_info, early_feats=feats)
    criterion3d = torch.nn.CrossEntropyLoss(ignore_index=0)
    loss3d = mvlifting.get_loss_3d(criterion3d, predictions_3d, seg, cls)
    loss = lambda_l2d * loss2d + lambda_l3d * loss3d
    return loss, predictions_3d

for epoch in range(nb_epochs):
    print(f"\nEpoch {epoch + 1}/{nb_epochs}")

    print("Training...")
    mvnetwork.train()
    mvtn.train()
    mvrenderer.train()
    running_loss = 0
    for i, (points, cls, seg, parts_range, parts_nb, _) in enumerate(train_loader):
        azim, elev, dist = mvtn(points, c_batch_size=len(points))
        view_info = torch.cat([azim.unsqueeze(-1), elev.unsqueeze(-1)], dim=-1)
        rendered_images, indxs, distance_weight_maps, _ = mvrenderer(None, points, azim=azim, elev=elev, dist=dist, color=None)

        cls, seg, points = cls.cuda(), seg.cuda(), points.cuda()

        seg = seg + 1 - parts_range[..., None].cuda().to(torch.int) if parallel_head else seg + 1

        labels_2d, pix_to_face_mask = mvlifting.compute_image_segment_label_points(points, batch_points_labels=seg, rendered_pix_to_point=indxs)
        labels_2d, rendered_images = labels_2d.cuda(), rendered_images.cuda()

        loss, predictions_3d = calc_loss_and_3d_pred(rendered_images, cls, labels_2d, azim, elev, points, indxs, parts_nb, view_info, seg)
        _, predictions_3d = torch.max(predictions_3d, dim=1)

        running_loss += loss.item()
        loss.backward()

        optimizer.step()
        optimizer.zero_grad()
        if mvtn_optimizer is not None:
            mvtn_optimizer.step()
            mvtn_optimizer.zero_grad()
        
        if (i + 1) % int(len(train_loader) * 0.25) == 0:
            print(f"\tBatch {i + 1}/{len(train_loader)}: Current Average Training Loss = {(running_loss / (i + 1)):.5f}")
    print(f"Total Average Training Loss = {(running_loss / len(train_loader)):.5f}")

    print("Testing...")
    mvnetwork.eval()
    mvtn.eval()
    mvrenderer.eval()
    running_loss = 0
    for i, (points, cls, seg, parts_range, parts_nb, _) in enumerate(test_loader):
        with torch.no_grad():
            azim, elev, dist = mvtn(points, c_batch_size=len(points))
            view_info = torch.cat([azim.unsqueeze(-1), elev.unsqueeze(-1)], dim=-1)
            rendered_images, indxs, distance_weight_maps, _ = mvrenderer(None, points, azim=azim, elev=elev, dist=dist, color=None)

            cls, seg, points = cls.cuda(), seg.cuda(), points.cuda()

            seg = seg + 1 - parts_range[..., None].cuda().to(torch.int)
            parts_range += 1

            labels_2d, pix_to_face_mask = mvlifting.compute_image_segment_label_points(points, batch_points_labels=seg, rendered_pix_to_point=indxs)

            loss, _ = calc_loss_and_3d_pred(rendered_images, cls, labels_2d, azim, elev, points, indxs, parts_nb, view_info, seg)

            running_loss += loss.item()
            if (i + 1) % int(len(test_loader) * 0.25) == 0:
                print(f"\tBatch {i + 1}/{len(test_loader)}: Current Average Test Loss = {(running_loss / (i + 1)):.5f}")
    print(f"Total Average Test Loss = {(running_loss / len(test_loader)):.5f}")