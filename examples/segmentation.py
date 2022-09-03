# to import files from parent dir
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
from mvtorch.data import PartNormalDataset, CustomDataLoader
from mvtorch.view_selector import MVTN, MVPartSegmentation, MVLiftingModule
from mvtorch.mvrenderer import MVRenderer
from mvtorch.networks import MLPClassifier
from mvtorch.ops import svctomvc, post_process_segmentation

# Create dataset and dataloader
dset_train = PartNormalDataset(root='./data/shapenet_part', split='trainval')
dset_val = PartNormalDataset(root='./data/shapenet_part', split='test')
train_loader = CustomDataLoader(dset_train, batch_size=5, shuffle=True, drop_last=True)
test_loader = CustomDataLoader(dset_train, batch_size=5, shuffle=False, drop_last=False)

# Create backbone multi-view network (DeepLabV3 with ResNet-101)
num_classes = len(dset_train.seg_classes)
num_parts = max(dset_train.parts_per_class)
mvnetwork = torch.hub.load('pytorch/vision:v0.10.0', 'deeplabv3_resnet101', pretrained=True)
mvnetwork = MVPartSegmentation(mvnetwork, num_classes=num_classes, num_parts=num_parts).cuda()

# Create backbone optimizer
optimizer = torch.optim.AdamW(mvnetwork.parameters(), lr=0.00001, weight_decay=0.03)

# Create view selector
nb_views = 1
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

epochs = 100
parallel_head = True
lambda_l2d = 1
lambda_l3d = 0
for epoch in range(epochs):
    print(f"\nEpoch {epoch + 1}/{epochs}")

    print("Training...")
    mvnetwork.train()
    mvtn.train()
    mvrenderer.train()
    running_loss = 0
    for i, (points, cls, seg, parts_range, parts_nb, _) in enumerate(train_loader):
        normals = points[:, :, 3:6]
        colors = (normals + 1.0) / 2.0
        colors = colors/torch.norm(colors, dim=-1,p=float('inf'))[..., None]
        points = points[:,:,0:3]

        azim, elev, dist = mvtn(points, c_batch_size=len(points))
        view_info = torch.cat([azim.unsqueeze(-1), elev.unsqueeze(-1)], dim=-1)
        rendered_images, indxs, distance_weight_maps, _ = mvrenderer(None, points, azim=azim, elev=elev, dist=dist, color=colors)

        cls = cls.cuda()
        cls = torch.autograd.Variable(cls)
        seg = seg.cuda()
        points = torch.autograd.Variable(points).cuda()
        seg = torch.autograd.Variable(seg)

        seg = seg + 1 - parts_range[..., None].cuda().to(torch.int) if parallel_head else seg + 1

        labels_2d, pix_to_face_mask = mvlifting.compute_image_segment_label_points(points, batch_points_labels=seg, rendered_pix_to_point=indxs)
        labels_2d = torch.autograd.Variable(labels_2d)

        rendered_images = torch.autograd.Variable(rendered_images)

        criterion2d = torch.nn.CrossEntropyLoss(ignore_index=0, reduction="none")
        outputs, feats = mvnetwork(rendered_images, cls)
        loss2d = mvnetwork.get_loss(criterion2d, outputs, labels_2d, cls)
        _, predicted = torch.max(outputs.data, dim=1) 
        views_weights = mvlifting.compute_views_weights(azim, elev, rendered_images, normals)
        predictions_3d = mvlifting.lift_2D_to_3D(points, predictions_2d=svctomvc(outputs, nb_views=nb_views), rendered_pix_to_point=indxs, views_weights=views_weights, cls=cls, parts_nb=parts_nb, view_info=view_info, early_feats=feats)
        criterion3d = torch.nn.CrossEntropyLoss(ignore_index=0, reduction="none")
        loss3d = mvlifting.get_loss_3d(criterion3d, predictions_3d, seg, cls)
        loss = lambda_l2d * loss2d + lambda_l3d * loss3d
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
    for i, (points, cls, seg, parts_range, parts_nb, real_points_mask) in enumerate(test_loader):
        with torch.no_grad():
            normals = points[:, :, 3:6]
            colors = (normals + 1.0) / 2.0
            colors = colors/torch.norm(colors, dim=-1,p=float('inf'))[..., None]
            points = points[:,:,0:3]

            azim, elev, dist = mvtn(points, c_batch_size=len(points))
            view_info = torch.cat([azim.unsqueeze(-1), elev.unsqueeze(-1)], dim=-1)
            rendered_images, indxs, distance_weight_maps, _ = mvrenderer(None, points, azim=azim, elev=elev, dist=dist, color=colors)

            cls = cls.cuda()
            cls = torch.autograd.Variable(cls)
            seg = seg.cuda()
            points = torch.autograd.Variable(points).cuda()
            seg = torch.autograd.Variable(seg)
            real_points_mask = real_points_mask.cuda()

            seg = seg + 1 - parts_range[..., None].cuda().to(torch.int)
            parts_range += 1

            labels_2d, pix_to_face_mask  = mvlifting.compute_image_segment_label_points(points, batch_points_labels=seg, rendered_pix_to_point=indxs)

            criterion = torch.nn.CrossEntropyLoss(ignore_index=0)
            outputs , feats = mvnetwork(rendered_images, cls)
            loss2d = mvnetwork.get_loss(criterion, outputs, labels_2d, cls)
            _, predicted = torch.max(outputs.data, dim=1)

            views_weights = mvlifting.compute_views_weights(azim, elev, rendered_images, normals) 

            predictions_3d = mvlifting.lift_2D_to_3D(points, predictions_2d=svctomvc(outputs, nb_views=nb_views), rendered_pix_to_point=indxs, views_weights=views_weights, cls=cls, parts_nb=parts_nb, view_info=view_info,early_feats=feats)
            criterion3d = torch.nn.CrossEntropyLoss(ignore_index=0,)
            loss3d = mvlifting.get_loss_3d(criterion3d, predictions_3d, seg, cls)
            loss = lambda_l2d * loss2d + lambda_l3d * loss3d

            running_loss += loss.item()

            if (i + 1) % int(len(test_loader) * 0.25) == 0:
                print(f"\tBatch {i + 1}/{len(test_loader)}: Current Average Test Loss = {(running_loss / (i + 1)):.5f}")
    print(f"Total Average Test Loss = {(running_loss / len(test_loader)):.5f}")