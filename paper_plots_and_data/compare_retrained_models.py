import numpy as np
import torch
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import sys
sys.path.append('../')
import validate
from utils.learning_helpers import save_obj, load_obj, data_and_model_loader
import os
from validate import compute_trajectory as tt
import glob
import csv

path_to_ws = '/home/brandon-wagstaff/learned_scale_recovery/'
path_to_dset_downsized = '/media/m2-drive/datasets/KITTI-odometry-downsized/'

seq_list = ['05', '09', '10'] 
method_list = ['Original', 'Retrained (unscaled)', 'Retrained (scaled)']

dir_list = [path_to_ws+'results/202007291515-oxford-scaled', \
    path_to_ws+'results/202008041450-oxford-to-kitti-1-epoch-unscaled', \
    path_to_ws+'results/202008041237-oxford-to-kitti-1-epoch-scaled', 
     ]

test_seq = '05'
val_seq = '00'
cam_height = 1.70 #1.52
plot_range =  slice(0,-1)
csv_header1 = ['Method', 'Mean Trans. Seg. Err. (\%)']
csv_header2 = [''] + seq_list + ['Mean']

with open('table4_retraining.csv', "w") as f:
    writer = csv.writer(f)
    writer.writerow(csv_header1)
    writer.writerow(csv_header2)

    for method, dir in zip(method_list, dir_list):
        seq_results = []
        for seq in seq_list:
            print('sequence: {}'.format(seq))
            results_dir = dir + '/results/scale/'
            config = load_obj('{}/config'.format(dir))
            config['test_seq'] = [seq]
            config['data_dir'] = path_to_dset_downsized+config['img_resolution'] + '_res/' #if grabbed from obelisk
            config['estimator'] = 'orbslam'
            dpc = config['dpc']
            mode = config['pose_output_type']
            if dpc:
                prefix = 'dpc'
            else:
                prefix=''
            test_dset_loaders, _, _ = data_and_model_loader(config, None, None, seq=seq)

            data = load_obj('{}/{}_plane_fit'.format(results_dir, config['test_seq'][0]))
            
            dist_to_plane = data['dist_to_plane']
            fwd_pose_vec1 = data['fwd_pose_vec1']
            fwd_pose_vec2 = data['fwd_pose_vec2']
            inv_pose_vec1 = data['inv_pose_vec1']
            inv_pose_vec2 = data['inv_pose_vec2']
            gt_pose_vec = data['gt_pose_vec']
            vo_pose_vec = data['vo_pose_vec']
            num_inliers = data['num_inliers']
            normals = data['normal']
            if config['dpc'] == False:
                prefix = ''
            if config['dpc'] == True:
                prefix = prefix = 'dpc-'

            d = [np.median(np.abs(i)) for i in dist_to_plane] 
            d  = np.array(d)
            average_d = np.average(d) 

            
            unscaled_pose_vec = fwd_pose_vec1
            # unscaled_pose_vec = -inv_pose_vec1
            # unscaled_pose_vec = (fwd_pose_vec1 - inv_pose_vec1)/2 
            unscaled_pose_vec[:,3:6] = gt_pose_vec[:,3:6]
            
            ## align with global scale
            gt_norm = np.linalg.norm(gt_pose_vec[:,0:3],axis=1)
            vo_norm = np.linalg.norm(vo_pose_vec[:,0:3],axis=1)
            vo_scale_factor = np.average(gt_norm/vo_norm)
            unscaled_pose_vec = unscaled_pose_vec[:,0:6]


            scaled_pose_vec = np.array(unscaled_pose_vec)
            scaled_pose_vec[:,0:3] = scaled_pose_vec[:,0:3]*np.repeat(cam_height/d.reshape((-1,1)),3,axis=1)
            vo_pose_vec[:,0:3] = vo_pose_vec[:,0:3]*vo_scale_factor
            
            ## Compute Trajectories
            gt_traj = test_dset_loaders.dataset.raw_gt_trials[0]
            est, gt, errors, cum_dist = tt(unscaled_pose_vec,gt_traj,method=method)
            seq_results.append(errors[2])
        
        errors = np.array(seq_results)
        mean = np.mean(errors)
        seq_results.append(mean)
        seq_results = ["%.2f" % e for e in seq_results]
        writer.writerow([method] + seq_results)
