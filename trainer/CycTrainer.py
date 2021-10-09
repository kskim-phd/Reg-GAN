#!/usr/bin/python3

import argparse
import itertools
import torchvision.transforms as transforms
from torch.utils.data import DataLoader
from torch.autograd import Variable
import torch
from .utils import LambdaLR,Logger,ReplayBuffer
from .utils import weights_init_normal,get_config
from .datasets import ImageDataset,ValDataset
from Model.CycleGan import *
from .utils import Resize,ToTensor,smooothing_loss
from .utils import Logger
from .reg import Reg
from torchvision.transforms import RandomAffine
from torchvision.transforms import RandomAffine,ToPILImage
from .transformer import Transformer_2D
from skimage import measure
import numpy as np
import cv2

class Cyc_Trainer():
    def __init__(self, config):
        super().__init__()
        self.config = config
        ## def networks
        self.netG_A2B = Generator(config['input_nc'], config['output_nc']).cuda()
        self.netD_B = Discriminator(config['input_nc']).cuda()
        self.optimizer_D_B = torch.optim.Adam(self.netD_B.parameters(), lr=config['lr'], betas=(0.5, 0.999))
        
        if config['regist']:
            self.R_A = Reg().cuda()
            self.spatial_transform = Transformer_2D().cuda()
            
            self.optimizer_R_A = torch.optim.Adam(self.R_A.parameters(), lr=config['lr'], betas=(0.5, 0.999))
        if config['bidirect']:
            self.netG_B2A = Generator(config['input_nc'], config['output_nc']).cuda()
            self.netD_A = Discriminator(config['input_nc']).cuda()
            self.optimizer_G = torch.optim.Adam(itertools.chain(self.netG_A2B.parameters(), self.netG_B2A.parameters()),lr=config['lr'], betas=(0.5, 0.999))
            self.optimizer_D_A = torch.optim.Adam(self.netD_A.parameters(), lr=config['lr'], betas=(0.5, 0.999))
            
            
        else:
            self.optimizer_G = torch.optim.Adam(self.netG_A2B.parameters(), lr=config['lr'], betas=(0.5, 0.999))
            
            
                
        # Lossess
        self.MSE_loss = torch.nn.MSELoss()
        self.L1_loss = torch.nn.L1Loss()

        # Inputs & targets memory allocation
        Tensor = torch.cuda.FloatTensor if config['cuda'] else torch.Tensor
        self.input_A = Tensor(config['batchSize'], config['input_nc'], config['size'], config['size'])
        self.input_B = Tensor(config['batchSize'], config['output_nc'], config['size'], config['size'])
        self.target_real = Variable(Tensor(1,1).fill_(1.0), requires_grad=False)
        self.target_fake = Variable(Tensor(1,1).fill_(0.0), requires_grad=False)

        self.fake_A_buffer = ReplayBuffer()
        self.fake_B_buffer = ReplayBuffer()

        #Dataset loader
        transforms_1 = [ToPILImage(),
                   RandomAffine(degrees=3,translate=[0.05, 0.05],scale=[0.95, 1.05],fillcolor=-1),#degrees=2,translate=[0.05, 0.05],scale=[0.9, 1.1]
                   ToTensor(),
                   Resize(size_tuple = (config['size'], config['size']))]
    
        transforms_2 = [ToPILImage(),
                   RandomAffine(degrees=3,translate=[0.05, 0.05],scale=[0.95, 1.05],fillcolor=-1),
                   ToTensor(),
                   Resize(size_tuple = (config['size'], config['size']))]

        self.dataloader = DataLoader(ImageDataset(config['dataroot'], transforms_1=transforms_1, transforms_2=transforms_2, unaligned=False),
                                batch_size=config['batchSize'], shuffle=True, num_workers=config['n_cpu'])
        
        
        val_transforms = [ToTensor(),
                    Resize(size_tuple = (config['size'], config['size']))]
        
        self.val_data = DataLoader(ValDataset(config['val_dataroot'], transforms_ =val_transforms, unaligned=False),
                                batch_size=config['batchSize'], shuffle=False, num_workers=config['n_cpu'])

 
       # Loss plot
        self.logger = Logger(config['name'],config['n_epochs'], len(self.dataloader))       
        
    def train(self):
        ###### Training ######
        for epoch in range(self.config['epoch'], self.config['n_epochs']):
            SM = 0
            for i, batch in enumerate(self.dataloader):
                # Set model input
                real_A = Variable(self.input_A.copy_(batch['T1']))
                real_B = Variable(self.input_B.copy_(batch['T2']))
                if self.config['bidirect']:   # b dir
                    if self.config['regist']:    # + reg 
                        self.optimizer_R_A.zero_grad()
                        self.optimizer_G.zero_grad()
                        # GAN loss
                        fake_B = self.netG_A2B(real_A)
                        pred_fake = self.netD_B(fake_B)
                        loss_GAN_A2B = self.MSE_loss(pred_fake, self.target_real)
                        
                        fake_A = self.netG_B2A(real_B)
                        pred_fake = self.netD_A(fake_A)
                        loss_GAN_B2A = self.MSE_loss(pred_fake, self.target_real)
                        
                        Trans = self.R_A(fake_B,real_B) 
                        SysRegist_A2B = self.spatial_transform(fake_B,Trans)
                        SR_loss = 20*self.L1_loss(SysRegist_A2B,real_B)###SR
                        SM_loss = 10*smooothing_loss(Trans)
                        
                        # Cycle loss
                        recovered_A = self.netG_B2A(fake_B)
                        loss_cycle_ABA = self.L1_loss(recovered_A, real_A) * 10.0

                        recovered_B = self.netG_A2B(fake_A)
                        loss_cycle_BAB = self.L1_loss(recovered_B, real_B) * 10.0

                        # Total loss
                        loss_G = loss_GAN_A2B + loss_GAN_B2A + loss_cycle_ABA + loss_cycle_BAB + SR_loss +SM_loss
                        loss_G.backward()
                        self.optimizer_G.step()
                        self.optimizer_R_A.step()
                        
                        ###### Discriminator A ######
                        self.optimizer_D_A.zero_grad()
                        # Real loss
                        pred_real = self.netD_A(real_A)
                        loss_D_real = self.MSE_loss(pred_real, self.target_real)
                        # Fake loss
                        fake_A = self.fake_A_buffer.push_and_pop(fake_A)
                        pred_fake = self.netD_A(fake_A.detach())
                        loss_D_fake = self.MSE_loss(pred_fake, self.target_fake)

                        # Total loss
                        loss_D_A = (loss_D_real + loss_D_fake)
                        loss_D_A.backward()

                        self.optimizer_D_A.step()
                        ###################################

                        ###### Discriminator B ######
                        self.optimizer_D_B.zero_grad()

                        # Real loss
                        pred_real = self.netD_B(real_B)
                        loss_D_real = self.MSE_loss(pred_real, self.target_real)

                        # Fake loss
                        fake_B = self.fake_B_buffer.push_and_pop(fake_B)
                        pred_fake = self.netD_B(fake_B.detach())
                        loss_D_fake = self.MSE_loss(pred_fake, self.target_fake)

                        # Total loss
                        loss_D_B = (loss_D_real + loss_D_fake)
                        loss_D_B.backward()

                        self.optimizer_D_B.step()
                        ################################### 
                    
                    else: 
                        self.optimizer_G.zero_grad()
                        # GAN loss
                        fake_B = self.netG_A2B(real_A)
                        pred_fake = self.netD_B(fake_B)
                        loss_GAN_A2B = self.MSE_loss(pred_fake, self.target_real)

                        fake_A = self.netG_B2A(real_B)
                        pred_fake = self.netD_A(fake_A)
                        loss_GAN_B2A = self.MSE_loss(pred_fake, self.target_real)

                        # Cycle loss
                        recovered_A = self.netG_B2A(fake_B)
                        loss_cycle_ABA = self.L1_loss(recovered_A, real_A) * 10.0

                        recovered_B = self.netG_A2B(fake_A)
                        loss_cycle_BAB = self.L1_loss(recovered_B, real_B) * 10.0

                        # Total loss
                        loss_G = loss_GAN_A2B + loss_GAN_B2A + loss_cycle_ABA + loss_cycle_BAB 
                        loss_G.backward()
                        self.optimizer_G.step()

                        ###### Discriminator A ######
                        self.optimizer_D_A.zero_grad()
                        # Real loss
                        pred_real = self.netD_A(real_A)
                        loss_D_real = self.MSE_loss(pred_real, self.target_real)
                        # Fake loss
                        fake_A = self.fake_A_buffer.push_and_pop(fake_A)
                        pred_fake = self.netD_A(fake_A.detach())
                        loss_D_fake = self.MSE_loss(pred_fake, self.target_fake)

                        # Total loss
                        loss_D_A = (loss_D_real + loss_D_fake)
                        loss_D_A.backward()

                        self.optimizer_D_A.step()
                        ###################################

                        ###### Discriminator B ######
                        self.optimizer_D_B.zero_grad()

                        # Real loss
                        pred_real = self.netD_B(real_B)
                        loss_D_real = self.MSE_loss(pred_real, self.target_real)

                        # Fake loss
                        fake_B = self.fake_B_buffer.push_and_pop(fake_B)
                        pred_fake = self.netD_B(fake_B.detach())
                        loss_D_fake = self.MSE_loss(pred_fake, self.target_fake)

                        # Total loss
                        loss_D_B = (loss_D_real + loss_D_fake)
                        loss_D_B.backward()

                        self.optimizer_D_B.step()
                        ###################################
                        
                        
                        
                else:                  # s dir
                    if self.config['regist']:    # + reg 
                        self.optimizer_R_A.zero_grad()
                        self.optimizer_G.zero_grad()
                        #### regist sys loss

                        fake_B = self.netG_A2B(real_A)
                        Trans = self.R_A(fake_B,real_B) 
                        SysRegist_A2B = self.spatial_transform(fake_B,Trans)
  

                        SR_loss = 20*self.L1_loss(SysRegist_A2B,real_B)###SR

                        
                        pred_fake0 = self.netD_B(fake_B)


                        adv_loss = self.MSE_loss(pred_fake0, self.target_real)
                        ####smooth loss
                        SM_loss = 10*smooothing_loss(Trans)

                        toal_loss = SM_loss+adv_loss+SR_loss

                        toal_loss.backward()
                        self.optimizer_R_A.step()
                        self.optimizer_G.step()
                        
                        self.optimizer_D_B.zero_grad()
                        with torch.no_grad():
                            fake_B = self.netG_A2B(real_A)
                            #Trans = self.R_A(fake_B,real_B)
                            #SysRegist_A2B = self.spatial_transform(fake_B,Trans)
                            

                        
                        pred_fake0 = self.netD_B(fake_B)
                        pred_real = self.netD_B(real_B)
                        loss_D_B = self.MSE_loss(pred_fake0, self.target_fake)+self.MSE_loss(pred_real, self.target_real)


                        loss_D_B.backward()
                        self.optimizer_D_B.step()
                        
                        
                        
                    else:
                        self.optimizer_G.zero_grad()
                        fake_B = self.netG_A2B(real_A)
                        #### GAN aligin loss
                        pred_fake = self.netD_B(fake_B)
                        adv_loss = self.MSE_loss(pred_fake, self.target_real)
                        adv_loss.backward()
                        self.optimizer_G.step()
                        ###### Discriminator B ######
                        self.optimizer_D_B.zero_grad()
                        # Real loss
                        pred_real = self.netD_B(real_B)
                        loss_D_real = self.MSE_loss(pred_real, self.target_real)
                        # Fake loss
                        fake_B = self.fake_B_buffer.push_and_pop(fake_B)
                        pred_fake = self.netD_B(fake_B.detach())
                        loss_D_fake = self.MSE_loss(pred_fake, self.target_fake)
                        # Total loss
                        loss_D_B = (loss_D_real + loss_D_fake)
                        loss_D_B.backward()
                        self.optimizer_D_B.step()
                        ###################################
                        #'adv_loss':adv_loss,'SM':SM_loss,'SR_loss':SR_loss

                self.logger.log({'loss_D_B': loss_D_B,},
                       images={'real_A': real_A, 'real_B': real_B, 'fake_B': fake_B,'regist_B': SysRegist_A2B,})#,'SR':SysRegist_A2B


                #sm = SM_loss.item()
                #SM = SM+sm
                
                    
                    
                
                
                
    #         # Save models checkpoints
            torch.save(self.netG_A2B.state_dict(), self.config['save_root'] + 'netG_A2B.pth')
            torch.save(self.R_A.state_dict(), self.config['save_root'] + 'Regist.pth')
            #torch.save(netD_A.state_dict(), 'output/netD_A_3D.pth')
            #torch.save(netD_B.state_dict(), 'output/netD_B_3D.pth')
            
            
            #############val###############
            with torch.no_grad():
                MAE = 0
                num = 0
                for i, batch in enumerate(self.val_data):
                    real_A = Variable(self.input_A.copy_(batch['T1']))
                    real_B = Variable(self.input_B.copy_(batch['T2'])).detach().cpu().numpy().squeeze()
                    fake_B = self.netG_A2B(real_A).detach().cpu().numpy().squeeze()
                    mae = self.MAE(fake_B,real_B)
                    MAE += mae
                    num += 1
                
                
                    
#                 LD = loss_D_B.item()
#                 ADV = toal_loss.item()
                
#                 #SR = SR_loss.item()
#                 L = [[LD,ADV,MAE/num,SM/5760]] #LD,ADV,SR,MAE/num
#                 arr = np.array(L)
                
#                 if epoch == 0:
#                     np.save(self.config['save_root']+'save', arr)
#                 else:
#                     old_save = np.load(self.config['save_root']+'save.npy')
#                     new_save = np.concatenate([old_save,arr],0)
                    
#                     np.save(self.config['save_root']+'save', new_save)
                    
                    
                    
                print ('MAE:',MAE/num)
                
                    
                         
    def test(self,):
        self.netG_A2B.load_state_dict(torch.load(self.config['save_root'] + 'netG_A2B.pth'))
        #self.R_A.load_state_dict(torch.load(self.config['save_root'] + 'Regist.pth'))
        
        with torch.no_grad():
                MAE = 0
                PSNR = 0
                SSIM = 0
                num = 0
                
                for i, batch in enumerate(self.val_data):
                    real_A = Variable(self.input_A.copy_(batch['T1']))
                    real_B = Variable(self.input_B.copy_(batch['T2']))#.detach().cpu().numpy().squeeze()
                    
                    fake_B = self.netG_A2B(real_A)#.detach().cpu().numpy().squeeze()
                    fake_B = fake_B.detach().cpu().numpy().squeeze()                                                 
                    mae = self.MAE(fake_B,real_B)
                    psnr = self.PSNR(fake_B,real_B)
                    ssim = measure.compare_ssim(fake_B,real_B)
                    
                    MAE += mae
                    PSNR += psnr
                    SSIM += ssim 
                    num += 1
                    
                    
#                     real_A = ((real_A.detach().cpu().numpy().squeeze()+1)/2)*255
#                     real_B = ((real_B+1)/2)*255
#                     image_FB = 255*((fake_B+1)/2)
#                     image_FB_regist = 255*((SysRegist_A2B+1)/2) 
                    
                    #cv2.imwrite(self.config['image_save']+'A/'+ str(num)+'.png',real_A)
                    #cv2.imwrite(self.config['image_save']+'B/'+ str(num)+'.png',real_B)   
#                     cv2.imwrite(self.config['image_save']+'fake_B/'+ str(num)+'.png',image_FB)
                    #cv2.imwrite(self.config['image_save']+'regist_B/'+ str(num)+'.png',image_FB_regist)                        
                    
                    #real_A = real_A.detach().cpu().numpy().squeeze()
                    #real_B = real_B.detach().cpu().numpy().squeeze()
                    #real_A  = 255*((real_A +1)/2)
                    #real_B  = 255*((real_B +1)/2)
                    
                    
                    #cv2.imwrite('/data1/T1T2_out/Val_A/'+ str(num)+'.png',real_A)
                    #cv2.imwrite('/data1/T1T2_out/Val_B/'+ str(num)+'.png',real_B)
                    
                    
                 
                print ('MAE:',MAE/num)
                print ('PSNR:',PSNR/num)
                print ('SSIM:',SSIM/num)
    
    def PSNR(self,fake,real):
       x,y = np.where(real!= -1)
       mse = np.mean(((fake[x][y]+1)/2. - (real[x][y]+1)/2.) ** 2 )
       if mse < 1.0e-10:
          return 100
       else:
           PIXEL_MAX = 1
           return 20 * np.log10(PIXEL_MAX / np.sqrt(mse))
            
            
    def MAE(self,fake,real):
        x,y = np.where(real!= -1)  # coordinate of target points
        #points = len(x)  #num of target points
        mae = np.abs(fake[x,y]-real[x,y]).mean()
            
        return mae/2    
            
            
            
    def save_deformation(self,defms,root):
        heatmapshow = None
        defms_ = defms.data.cpu().float().numpy()
        dir_x = defms_[0]
        dir_y = defms_[1]
        x_max,x_min = dir_x.max(),dir_x.min()
        y_max,y_min = dir_y.max(),dir_y.min()
        dir_x = ((dir_x-x_min)/(x_max-x_min))*255
        dir_y = ((dir_y-y_min)/(y_max-y_min))*255
        #print (x_max,x_min)
        #print (y_max,y_min)
        tans_x = cv2.normalize(dir_x, heatmapshow, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_8U)
        tans_x[tans_x<=150] = 0
        #print (tans_x.shape,tans_x.max(),tans_x.min())
        tans_x = cv2.applyColorMap(tans_x, cv2.COLORMAP_JET)
        tans_y = cv2.normalize(dir_y, heatmapshow, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_8U)
        tans_y[tans_y<=150] = 0
        tans_y = cv2.applyColorMap(tans_y, cv2.COLORMAP_JET)
        gradxy = cv2.addWeighted(tans_x, 0.5,tans_y, 0.5, 0)

        cv2.imwrite(root, gradxy) 
