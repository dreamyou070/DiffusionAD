from random import seed
import torch
import os
import json
import numpy as np
from torch import optim
from torch.utils.data import DataLoader
from models.Recon_subnetwork import UNetModel
from models.Seg_subnetwork import SegmentationSubNetwork
from tqdm import tqdm
import torch.nn as nn
from data.dataset_beta_thresh import MVTecTrainDataset,MVTecTestDataset
import torch.nn.functional as F
from models.DDPM import GaussianDiffusionModel, get_beta_schedule
from sklearn.metrics import roc_auc_score,auc,average_precision_score
import pandas as pd
from collections import defaultdict

def weights_init(m):
        classname = m.__class__.__name__
        if classname.find('Conv') != -1:
            m.weight.data.normal_(0.0, 0.02)
        elif classname.find('BatchNorm') != -1:
            m.weight.data.normal_(1.0, 0.02)
            m.bias.data.fill_(0)    

def defaultdict_from_json(jsonDict):
    func = lambda: defaultdict(str)
    dd = func()
    dd.update(jsonDict)
    return dd

class BinaryFocalLoss(nn.Module):
    def __init__(self, alpha=0.5, gamma=4, logits=False, reduce=True): 
        super(BinaryFocalLoss, self).__init__()
        self.alpha = alpha
        self.gamma = gamma
        self.logits = logits
        self.reduce = reduce

    def forward(self, inputs, targets):
        if self.logits:
            BCE_loss = F.binary_cross_entropy_with_logits(inputs, targets, reduction='none')
        else:
            BCE_loss = F.binary_cross_entropy(inputs, targets, reduction='none')
        pt = torch.exp(-BCE_loss)
        F_loss = self.alpha * (1-pt)**self.gamma * BCE_loss

        if self.reduce:
            return torch.mean(F_loss)
        else:
            return F_loss

def train(training_dataset_loader, testing_dataset_loader, args, data_len,sub_class,class_type,device ):

    in_channels = args["channels"]
    unet_model = UNetModel(args['img_size'][0],
                           args['base_channels'],                    # 128
                           channel_mults=args['channel_mults'],
                           dropout=args["dropout"],
                           n_heads=args["num_heads"], n_head_channels=args["num_head_channels"],
                           in_channels=in_channels).to(device)

    betas = get_beta_schedule(args['T'], args['beta_schedule'])
    ddpm_sample = GaussianDiffusionModel(args['img_size'], betas, loss_weight=args['loss_weight'],
                                          loss_type=args['loss-type'], noise=args["noise_fn"],
                                          img_channels=in_channels)

    seg_model=SegmentationSubNetwork(in_channels=6,
                                     out_channels=1,).to(device)


    optimizer_ddpm = optim.Adam( unet_model.parameters(),lr=args['diffusion_lr'],weight_decay=args['weight_decay'])

    optimizer_seg = optim.Adam(seg_model.parameters(),lr=args['seg_lr'],weight_decay=args['weight_decay'])



    loss_focal = BinaryFocalLoss().to(device)
    loss_smL1= nn.SmoothL1Loss().to(device)


    tqdm_epoch = range(0, args['EPOCHS'] )
    scheduler_seg =optim.lr_scheduler.CosineAnnealingLR(optimizer_seg, T_max=10, eta_min=0, last_epoch=- 1, verbose=False)

    # dataset loop
    train_loss_list=[]
    train_noise_loss_list=[]
    train_focal_loss_list=[]
    train_smL1_loss_list=[]
    loss_x_list=[]
    best_image_auroc=0.0
    best_pixel_auroc=0.0
    best_epoch=0
    image_auroc_list=[]
    pixel_auroc_list=[]
    performance_x_list=[]

    for epoch in tqdm_epoch:
        unet_model.train()
        seg_model.train()
        train_loss = 0.0
        train_focal_loss=0.0
        train_smL1_loss = 0.0
        train_noise_loss = 0.0
        tbar = tqdm(training_dataset_loader)
        for i, sample in enumerate(tbar):

            # -----------------------------------------------------------------------------------------------------------
            # [1] get sample
            aug_image=sample['augmented_image'].to(device)              # [batch, 3, 256, 256]
            anomaly_mask = sample["anomaly_mask"].to(device)            # [batch, 1, 256, 256]
            anomaly_label = sample["has_anomaly"].to(device).squeeze()  # [batch]

            noise_loss, pred_x0,normal_t, x_normal_t, x_noiser_t = ddpm_sample.norm_guided_one_step_denoising(unet_model,
                                                                            aug_image, anomaly_label,args)
            seg_input = torch.cat((aug_image, pred_x0), dim=1)          # [batch, 6, 64, 64]
            print(f'seg_input shape (batch, 6, 64, 64) : {seg_input.shape}')
            pred_mask = seg_model(torch.cat((aug_image, pred_x0), dim=1))
            print(f'pred_mask shape (batch, 1, 64, 64) : {pred_mask.shape}')

            #loss
            focal_loss = loss_focal(pred_mask,anomaly_mask)
            smL1_loss = loss_smL1(pred_mask, anomaly_mask)
            loss = noise_loss + 5*focal_loss + smL1_loss

            optimizer_ddpm.zero_grad()
            optimizer_seg.zero_grad()
            loss.backward()

            optimizer_ddpm.step()
            optimizer_seg.step()
            scheduler_seg.step()

            train_loss += loss.item()
            tbar.set_description('Epoch:%d, Train loss: %.3f' % (epoch, train_loss))

            train_smL1_loss += smL1_loss.item()
            train_focal_loss+=5*focal_loss.item()
            train_noise_loss+=noise_loss.item()


        if epoch % 10 ==0  and epoch > 0:
            train_loss_list.append(round(train_loss,3))
            train_smL1_loss_list.append(round(train_smL1_loss,3))
            train_focal_loss_list.append(round(train_focal_loss,3))
            train_noise_loss_list.append(round(train_noise_loss,3))
            loss_x_list.append(int(epoch))


        if (epoch+1) % 50==0 and epoch > 0:
            temp_image_auroc,temp_pixel_auroc= eval(testing_dataset_loader,args,unet_model,seg_model,data_len,sub_class,device)
            image_auroc_list.append(temp_image_auroc)
            pixel_auroc_list.append(temp_pixel_auroc)
            performance_x_list.append(int(epoch))
            if(temp_image_auroc+temp_pixel_auroc>=best_image_auroc+best_pixel_auroc):
                if temp_image_auroc>=best_image_auroc:
                    save(unet_model,seg_model, args=args,final='best',epoch=epoch,sub_class=sub_class)
                    best_image_auroc = temp_image_auroc
                    best_pixel_auroc = temp_pixel_auroc
                    best_epoch = epoch


    save(unet_model,seg_model, args=args,final='last',epoch=args['EPOCHS'],sub_class=sub_class)



    temp = {"classname":[sub_class],"Image-AUROC": [best_image_auroc],"Pixel-AUROC":[best_pixel_auroc],"epoch":best_epoch}
    df_class = pd.DataFrame(temp)
    df_class.to_csv(f"{args['output_path']}/metrics/ARGS={args['arg_num']}/{args['eval_normal_t']}_{args['eval_noisier_t']}t_{args['condition_w']}_{class_type}_image_pixel_auroc_train.csv", mode='a',header=False,index=False)

    

def eval(testing_dataset_loader,args,unet_model,seg_model,data_len,sub_class,device):
    unet_model.eval()
    seg_model.eval()
    os.makedirs(f'{args["output_path"]}/metrics/ARGS={args["arg_num"]}/{sub_class}/', exist_ok=True)
    in_channels = args["channels"]
    betas = get_beta_schedule(args['T'], args['beta_schedule'])

    ddpm_sample =  GaussianDiffusionModel(
            args['img_size'], betas, loss_weight=args['loss_weight'],
            loss_type=args['loss-type'], noise=args["noise_fn"], img_channels=in_channels
            )
    
    print("data_len",data_len)
    total_image_pred = np.array([])
    total_image_gt =np.array([])
    total_pixel_gt=np.array([])
    total_pixel_pred = np.array([])
    tbar = tqdm(testing_dataset_loader)
    for i, sample in enumerate(tbar):
        image = sample["image"].to(device)
        target=sample['has_anomaly'].to(device)
        gt_mask = sample["mask"].to(device)

        normal_t_tensor = torch.tensor([args["eval_normal_t"]], device=image.device).repeat(image.shape[0])
        noiser_t_tensor = torch.tensor([args["eval_noisier_t"]], device=image.device).repeat(image.shape[0])
        loss,pred_x_0_condition,pred_x_0_normal,pred_x_0_noisier,x_normal_t,x_noiser_t,pred_x_t_noisier = ddpm_sample.norm_guided_one_step_denoising_eval(unet_model, image, normal_t_tensor,noiser_t_tensor,args)
        pred_mask = seg_model(torch.cat((image, pred_x_0_condition), dim=1)) 

        out_mask = pred_mask

        topk_out_mask = torch.flatten(out_mask[0], start_dim=1)
        topk_out_mask = torch.topk(topk_out_mask, 50, dim=1, largest=True)[0]
        image_score = torch.mean(topk_out_mask)
        
        total_image_pred=np.append(total_image_pred,image_score.detach().cpu().numpy())
        total_image_gt=np.append(total_image_gt,target[0].detach().cpu().numpy())


        flatten_pred_mask=out_mask[0].flatten().detach().cpu().numpy()
        flatten_gt_mask =gt_mask[0].flatten().detach().cpu().numpy().astype(int)
            
        total_pixel_gt=np.append(total_pixel_gt,flatten_gt_mask)
        total_pixel_pred=np.append(total_pixel_pred,flatten_pred_mask)
        
        
    print(sub_class)
    auroc_image = round(roc_auc_score(total_image_gt,total_image_pred),3)*100
    print("Image AUC-ROC: " ,auroc_image)
    
    auroc_pixel =  round(roc_auc_score(total_pixel_gt, total_pixel_pred),3)*100
    print("Pixel AUC-ROC:" ,auroc_pixel)
   
    return auroc_image,auroc_pixel


def save(unet_model,seg_model, args,final,epoch,sub_class):
    
    if final=='last':
        torch.save(
            {
                'n_epoch':              epoch,
                'unet_model_state_dict': unet_model.state_dict(),
                'seg_model_state_dict':  seg_model.state_dict(),
                "args":                 args
                }, f'{args["output_path"]}/model/diff-params-ARGS={args["arg_num"]}/{sub_class}/params-{final}.pt'
            )
    
    else:
        torch.save(
                {
                    'n_epoch':              epoch,
                    'unet_model_state_dict': unet_model.state_dict(),
                    'seg_model_state_dict':  seg_model.state_dict(),
                    "args":                 args
                    }, f'{args["output_path"]}/model/diff-params-ARGS={args["arg_num"]}/{sub_class}/params-{final}.pt'
                )
    
    

def main(args):

    print(f' step 1. set class')
    mvtec_classes = ['carpet', 'grid', 'leather', 'tile', 'wood', 'bottle', 'cable', 'capsule', 'hazelnut', 'metal_nut',
                     'pill', 'screw', 'toothbrush', 'transistor', 'zipper']
    current_classes = mvtec_classes


    for sub_class in current_classes:
        print(f' (2.1) dataset of {sub_class}')
        subclass_path = os.path.join(args["mvtec_root_path"],sub_class)
        training_dataset = MVTecTrainDataset(subclass_path,
                                             sub_class,
                                             img_size=args["img_size"],args=args)
        testing_dataset = MVTecTestDataset(subclass_path,sub_class,img_size=args["img_size"],)
        class_type='MVTec'

        print(file, args)
        data_len = len(testing_dataset) 
        training_dataset_loader = DataLoader(training_dataset, batch_size=args['Batch_Size'],shuffle=True,
                                             num_workers=8,pin_memory=True,drop_last=True)
        test_loader = DataLoader(testing_dataset, batch_size=1,shuffle=False, num_workers=4)

        # make arg specific directories
        for i in [f'{args["output_path"]}/model/diff-params-ARGS={args["arg_num"]}/{sub_class}',
                  f'{args["output_path"]}/diffusion-training-images/ARGS={args["arg_num"]}/{sub_class}',
                  f'{args["output_path"]}/metrics/ARGS={args["arg_num"]}/{sub_class}']:
            try:
                os.makedirs(i)
            except OSError:
                pass
        train(training_dataset_loader, test_loader, args, data_len,sub_class,class_type,device )

if __name__ == '__main__':
    
    seed(42)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    file = "args1.json"
    with open(f'args/{file}', 'r') as f:
        args = json.load(f)
    args['arg_num'] = file[4:-5]
    args = defaultdict_from_json(args)
    main(args)
    """
    {
  "img_size": [256,256],
  "Batch_Size": 16,
  "EPOCHS": 3000,
  "T": 1000,
  "base_channels": 128,
  "beta_schedule": "linear",
  "loss_type": "l2",
  "diffusion_lr": 1e-4,
  "seg_lr": 1e-5,
  "random_slice": true,
  "weight_decay": 0.0,
  "save_imgs":true,
  "save_vids":false, 
  "dropout":0,
  "attention_resolutions":"32,16,8",
  "num_heads":4,
  "num_head_channels":-1,
  "noise_fn":"gauss",
  "channels":3,
  "mvtec_root_path":"datasets/mvtec",
  "visa_root_path":"datasets/VisA_1class/1cls",
  "dagm_root_path":"datasets/dagm",
  "mpdd_root_path":"datasets/mpdd",
  "anomaly_source_path":"datasets/DTD",
  "noisier_t_range":600,
  "less_t_range":300,
  "condition_w":1,
  "eval_normal_t":200,
  "eval_noisier_t":400,
  "output_path":"outputs"
}
    """