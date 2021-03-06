import torch
import sys
sys.path.insert(0,'..')
from data.kitti_loader import KittiLoaderPytorch
from train_plane import Plain_Trainer
from validate import get_plane_masks
import models.stn as stn
from models.plane_net import PlaneModel
from utils.learning_helpers import *
from utils.custom_transforms import *
import losses
from vis import *
import numpy as np
import datetime
import time
from tensorboardX import SummaryWriter
import argparse
import scipy.io as sio
import torch.backends.cudnn as cudnn
import os
import glob

device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
parser = argparse.ArgumentParser(description='training arguments.')

'''System Options'''
parser.add_argument('--estimator', type=str, default='orbslam') #libviso2 or orbslam
parser.add_argument('--estimator_type', type=str, default='mono') #mono or stereo
parser.add_argument('--flow_type', type=str, default='classical', help='classical, learned, none')
parser.add_argument('--preprocess_flow', action='store_true', default=False, help='only valid for classical flow')
parser.add_argument('--load_stereo', action='store_true', default=False)
parser.add_argument('--num_scales', type=int, default=3)
parser.add_argument('--img_resolution', type=str, default='med') # low (128x445) med (192 x640) or high (256 x 832) 
parser.add_argument('--img_per_sample', type=int, default=3) #1 target image, and rest are source images 
parser.add_argument('--pose_output_type', type=str, default='translation') # translation or pose
parser.add_argument('--dpc', action='store_true', default=False) # apply dpc to rotations - only works if pose_output_type is pose

'''Training Arguments'''
parser.add_argument('--data_dir', type=str, default='/media/m2-drive/datasets/KITTI-downsized')
parser.add_argument('--date', type=str, default='0000000')
parser.add_argument('--train_seq', nargs='+', type=str, default=['00'])
parser.add_argument('--val_seq', nargs='+',type=str, default=['00'])
parser.add_argument('--test_seq', nargs='+', type=str, default=['00'])
parser.add_argument('--augment_motion', action='store_true', default=False)
parser.add_argument('--wd', type=float, default=0)
parser.add_argument('--lr', type=float, default=9e-4)
parser.add_argument('--num_epochs', type=int, default=20)
parser.add_argument('--lr_decay_epoch', type=float, default=4)
parser.add_argument('--dropout_prob', type=float, default=0.3)
parser.add_argument('--save_results', action='store_true', default=True)
parser.add_argument('--max_depth', type=float, default=2) 
parser.add_argument('--min_depth', type=float, default=0.06) 
''' Losses'''

parser.add_argument('--pretrained_dir', type=str, default='')  
        
args = parser.parse_args()
config={
    'num_frames': None,
    'skip':1,    ### if not one, we skip every 'skip' samples that are generated ({1,2}, {2,3}, {3,4} becomes {1,2}, {3,4})
    'correction_rate': 1, ### if not one, only perform corrections every 'correction_rate' frames (samples become {1,3},{3,5},{5,7} when 2)
    'minibatch':15,      ##minibatch size      
    'load_pretrained_depth': True,
    'freeze_depthnet': True,
    }
for k in args.__dict__:
    config[k] = args.__dict__[k]
print(config)
print(args.train_seq, args.test_seq, args.val_seq)
args.data_dir = '{}/{}_res'.format(args.data_dir, config['img_resolution'])
config['data_dir'] = '{}/{}_res'.format(config['data_dir'], config['img_resolution'])
dsets = {x: KittiLoaderPytorch(config, [args.train_seq, args.val_seq, args.test_seq], mode=x, transform_img=get_data_transforms(config)[x], \
                               augment=config['augment_motion'], skip=config['skip']) for x in ['train', 'val']}
dset_loaders = {x: torch.utils.data.DataLoader(dsets[x], batch_size=config['minibatch'], shuffle=True, num_workers=8) for x in ['train', 'val']}

val_dset = KittiLoaderPytorch(config, [args.train_seq, args.val_seq, args.test_seq], mode='val', transform_img=get_data_transforms(config)['val'])
val_dset_loaders = torch.utils.data.DataLoader(val_dset, batch_size=config['minibatch'], shuffle=False, num_workers=8)

test_dset = KittiLoaderPytorch(config, [args.train_seq, args.val_seq, args.test_seq], mode='test', transform_img=get_data_transforms(config)['test'])
test_dset_loaders = torch.utils.data.DataLoader(test_dset, batch_size=config['minibatch'], shuffle=False, num_workers=8)

eval_dsets = {'val': val_dset_loaders, 'test':test_dset_loaders}
def main():
    results = {}
    results['pose_output_type'] = config['pose_output_type']
    results['estimator'] = config['estimator_type']
    config['device'] = device
    start = time.time()
    now= datetime.datetime.now()
    ts = '{}-{}-{}-{}-{}'.format(now.year, now.month, now.day, now.hour, now.minute)
    print(ts)
    if config['dpc']: print("Using DPC Framework")
    
    ''' Load Pretrained Models'''
    pretrained_depth_path, pretrained_pose_path = None, None
    if config['load_pretrained_depth']:
        pretrained_depth_path = glob.glob('{}/**depth**best-loss-val_seq-**-test_seq-**.pth'.format(config['pretrained_dir']))[0]
    
    epochs = range(0,config['num_epochs'])
        
    _, models, _ = data_and_model_loader(config, pretrained_depth_path, pretrained_pose_path)
    depth_model, pose_model = models
    
    if config['freeze_depthnet']: print('Freezing depth network weights.')
    for param in depth_model.parameters():
        param.requires_grad = not config['freeze_depthnet']         
   
        
    plane_model = PlaneModel(config).to(device)  
    models = [depth_model, plane_model]
    del(pose_model)
    params = list(plane_model.parameters())
    optimizer = torch.optim.Adam(params, lr=config['lr'], weight_decay = config['wd']) #, amsgrad=True)
    train_plane = Plain_Trainer(config, device, models, optimizer)
    cudnn.benchmark = True

    '''initialize variables'''
    losses_stacked = {}
    best_val_loss = {}
    best_loss_epoch = {}
    for phase, dset in eval_dsets.items():
        losses_stacked[phase] = np.empty((0, eval_dsets[phase].dataset.raw_gt_trials[0].shape[0]-config['img_per_sample']+1))
        best_val_loss[phase]= 1e5
        best_loss_epoch[phase]=0

    for epoch in epochs:
        optimizer = exp_lr_scheduler(optimizer, epoch, lr_decay_epoch=config['lr_decay_epoch']) ## reduce learning rate as training progresses  
        print("Epoch {}".format(epoch))
        
        train_loss = train_plane.forward(dset_loaders['train'], epoch, 'train')
        val_loss = train_plane.forward(dset_loaders['val'], epoch, 'val')
        if epoch == 0:
            writer = SummaryWriter(comment="-val_seq-{}-test_seq-{}".format(args.val_seq[0], args.test_seq[0]))
        writer.add_scalar('train', train_loss, epoch+1)
        writer.add_scalar('val', val_loss, epoch+1)
        
 
        for phase, dset in eval_dsets.items():
            if (phase == 'val' or phase == 'test'): 
                img_array, masks = get_plane_masks(device, models, dset, config, epoch=epoch)
                img_array = plot_img_array(img_array)
                writer.add_image(phase+'/imgs',img_array,epoch+1) 
                exp_mask = plot_img_array(masks)
                writer.add_image(phase+'/exp_mask', exp_mask, epoch+1)
                                    

                results[phase] = {'val_seq': args.val_seq, 
                    'test_seq': args.test_seq,
                    'epochs': epoch+1,
                    'est_traj_reconstruction_loss': losses_stacked[phase],
                }
                
                if args.save_results:   
                    ##Save the best models to the pretrained folder
                    
                    if val_loss < best_val_loss[phase] and epoch > 0:
                        best_val_loss[phase] = val_loss
                        best_loss_epoch[phase] = epoch
                        plane_dict_loss = plane_model.state_dict()
                        if phase == 'val':
                            print("Lowest validation loss (saving model)")       
                            torch.save(plane_dict_loss, '{}/plane-best-loss-val_seq-{}-test_seq-{}.pth'.format(config['pretrained_dir'], args.val_seq[0], args.test_seq[0]))

                    results[phase]['best_loss_epoch'] = best_loss_epoch[phase]
                    save_obj(results, '{}/plane-results-val_seq-{}-test_seq-{}'.format(config['pretrained_dir'], args.val_seq[0], args.test_seq[0]))
                    save_obj(config, '{}/plane-config'.format(config['pretrained_dir']))
                    f = open("{}/plane-config.txt".format(config['pretrained_dir']),"w")
                    f.write( str(config) )
                    f.close()


    duration = timeSince(start)    
    print("Training complete (duration: {})".format(duration))
 
main()
