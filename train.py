import os
import argparse

import numpy as np
import torch
from torch.nn import MSELoss
import matplotlib.pyplot as plt
from tqdm import tqdm

from network.profen import ProFEN
from network.transform import Affine2dPredictor, Affine2dPredictorSlim, Affine2dTransformer
from network.loss import RefInfoNCELoss, InfoNCELoss
from dataloaders import ProbeSingleCaseDataloader
from probe import ProbeGroup
from utils import time_it
from agent import AgentTask
import paths
from dataloaders import set_fold


class BaseTrainer:
    def __init__(self, model_name, model, save_dir, 
                 draw_loss=True, save_cycle=0, n_epoch=300, n_iter=None):
        """
        Args:
            model_name (str): 
            model (torch.nn.Module): 
            save_dir (str): Where to save the weights and training loss information.
            draw_loss (bool, optional): Draw the loss curve save to `loss.png`. Defaults to True.
            save_cycle (int, optional): Save the weights every cycle. Defaults to 0.
            n_epoch (int, optional): Defaults to 300.
            n_iter (int, optional): Defaults to 100.
        """
        self.model_name = model_name
        self.model = model
        self.save_dir = save_dir
        self.draw_loss = draw_loss
        self.save_cycle = save_cycle
        self.n_epoch = n_epoch
        self.n_iter = n_iter            # should be set by num_total / batch_size
        os.makedirs(save_dir, exist_ok=True)
    
    def train_iter(self) -> float:
        pass
    
    def train_epoch(self, epoch):
        epoch_losses = []
        for iter in range(self.n_iter):
            iter_loss = self.train_iter()
            epoch_losses.append(iter_loss)
        print('Epoch [{}/{}], average loss: {}'.format(epoch + 1, self.n_epoch, np.asarray(epoch_losses).mean()))
        if (self.save_cycle != 0) and ((epoch + 1) % self.save_cycle == 0):
            torch.save(self.model.state_dict(), '{}/{}.pth'.format(self.save_dir, epoch))
        torch.save(self.model.state_dict(), '{}/last.pth'.format(self.save_dir))
        return np.asarray(epoch_losses)
    
    @time_it
    def train(self, resume=False):
        """ Call train_epoch
        Args:
            resume (bool, optional): Resume from the last epoch. Defaults to False.
        """
        print('===> Training {}'.format(self.model_name))
        print('Save directory is {}.'.format(self.save_dir))
        self.model.cuda()
        self.model.train()
        if resume:
            train_losses = np.load('{}/loss.npy'.format(self.save_dir))
            if len(train_losses) == 1 or len(train_losses[-1]) != len(train_losses[0]):
                train_losses = train_losses[:-1]
            last_epoch = len(train_losses)
            self.model.load_state_dict(torch.load('{}/last.pth'.format(self.save_dir)))
            print('Resumed from epoch {}.'.format(last_epoch))
        else:
            last_epoch = 0
            train_losses = np.zeros((0, self.n_iter))
        best_loss = np.inf
        for epoch in tqdm(iterable=range(last_epoch, self.n_epoch), desc='Training epoch'):
            epoch_losses = self.train_epoch(epoch)
            if epoch_losses.mean() < best_loss:
                torch.save(self.model.state_dict(), '{}/best.pth'.format(self.save_dir))
                best_loss = epoch_losses.mean()
            train_losses = np.append(train_losses, [epoch_losses], axis=0)
            np.save('{}/loss.npy'.format(self.save_dir), train_losses)
        if self.draw_loss:
            hor_axis = np.arange(len(train_losses))
            average = train_losses.mean(-1)
            std = train_losses.std(-1)
            minus_half_std = average - std / 2
            add_half_std = average + std / 2
            plt.figure()
            plt.plot(hor_axis, average, color='black')            
            plt.fill_between(hor_axis, minus_half_std, add_half_std, color='lightgray', alpha=0.5)
            plt.title('{} Loss Curve'.format(self.model_name.upper()))
            plt.xlabel('epoch')
            plt.ylabel('loss')
            plt.savefig('{}/loss.png'.format(self.save_dir))
            plt.close()
        print('===> Training {} done.'.format(self.model_name))
        return


class ProfenTrainer(BaseTrainer):
    def __init__(self, ablation=None, fold=0, n_folds=0, batch_size=8, **kwargs):
        """
        Args:
            ablation (str, optional):
                'wo_ref_loss': use original InfoNCE loss rather than RefInfoNCE
                'wo_agent':
                'div_4': use 1/4 of the probes
                'div_9': use 1/9 of the probes
                'div_16': use 1/16 of the probes
        """
        model = ProFEN()
        model_name = 'profen' if ablation is None else 'profen_' + ablation
        save_dir = os.path.join(paths.WEIGHTS_DIR, 'fold{}'.format(fold), model_name)
        self.with_ref_loss = ablation != 'wo_ref_loss'
        self.with_agent = ablation != 'wo_agent'
        self.loss_func = RefInfoNCELoss().cuda() if self.with_ref_loss else InfoNCELoss().cuda()
        train_cases, _ = set_fold(fold, n_folds)
        probe_groups = [ProbeGroup(deserialize_path=os.path.join(paths.RESULTS_DIR, case_id, paths.PROBE_FILENAME))
            for case_id in train_cases]
        if ablation is not None and ablation.startswith('div_'):
            for pg in probe_groups:
                pg.sparse(factor=int(ablation[4:]))
        probes = [pg.probes for pg in probe_groups]
        self.dataloader = ProbeSingleCaseDataloader(probes=probes, batch_size=batch_size)
        self.batch_size = batch_size
        n_iter = self.dataloader.num_total // batch_size
        self.agent = AgentTask(occlusion_dir=paths.MASK_DIR)
        super().__init__(model_name=model_name, model=model, save_dir=save_dir, n_iter=n_iter, **kwargs)
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=1e-4)
                
    def train_iter(self) -> float:
        self.optimizer.zero_grad()
        batch = next(self.dataloader)
        render = torch.from_numpy(batch['data']).float().cuda()
        noise = self.agent.apply(render) if self.with_agent else render
        features = self.model(torch.cat([render, noise], dim=0))
        if self.with_ref_loss:
            positions = torch.from_numpy(batch['position']).float().cuda()
            loss = self.loss_func(features[:len(features) // 2], features[len(features) // 2:], positions)
        else:
            loss = self.loss_func(features[:len(features) // 2], features[len(features) // 2:])
        loss.backward()
        self.optimizer.step()
        return float(loss)


class Affine2DTrainer(BaseTrainer):
    def __init__(self, ablation=None, fold=0, n_folds=0, batch_size=8, **kwargs):
        model = Affine2dPredictorSlim()
        model_name = 'affine2d' if ablation is None else 'affine2d_' + ablation
        save_dir = os.path.join(paths.WEIGHTS_DIR, 'fold{}'.format(fold), model_name)
        self.transformer = Affine2dTransformer().cuda()
        self.loss_func = MSELoss()
        train_cases, _ = set_fold(fold, n_folds)
        probe_groups = [ProbeGroup(deserialize_path=os.path.join(paths.RESULTS_DIR, case_id, paths.PROBE_FILENAME))
            for case_id in train_cases]
        if ablation is not None and ablation.startswith('div_'):
            for pg in probe_groups:
                pg.sparse(factor=int(ablation[4:]))
        probes = [pg.probes for pg in probe_groups]
        self.dataloader = ProbeSingleCaseDataloader(probes=probes, batch_size=batch_size)
        self.batch_size = batch_size
        n_iter = self.dataloader.num_total // batch_size
        self.agent = AgentTask(occlusion_dir=paths.MASK_DIR)
        super().__init__(model_name=model_name, model=model, save_dir=save_dir, n_iter=n_iter, **kwargs)
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=1e-4)
        
    def train_iter(self) -> float:
        self.optimizer.zero_grad()
        batch = next(self.dataloader)
        render = torch.from_numpy(batch['data']).float().cuda()
        target = self.agent.apply(render)
        params = self.agent.real_params
        pred_params = self.model(render, target)
        pred_target = self.transformer(render, pred_params)
        # TODO: should compute the loss without mask
        # TODO: treat the channels differently
        loss_cycle = self.loss_func(target, pred_target)
        loss_param = self.loss_func(params, pred_params)
        loss = 0.5 * loss_cycle + 0.5 * loss_param
        loss.backward()
        self.optimizer.step()
        return float(loss)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Training parameters')
    parser.add_argument('-g', '--gpu', type=int, default=0, required=False, 
                        help='train on which gpu')
    parser.add_argument('-f', '--folds', type=int, nargs='+', default=[0, 1, 2, 3], required=False, 
                        help='which folds should be trained, e.g. --folds 0 2 4')
    parser.add_argument('-nf', '--n_folds', type=int, default=4, required=False, 
                        help='how many folds in total')
    parser.add_argument('-a', '--ablations', type=str, nargs='+', 
                        default=['none', 'div_4', 'div_9', 'div_16', 'wo_ref_loss', 'wo_agent'], required=False, 
                        help='which ablations to train, choices: none, div_4, div_9, div_16, wo_ref_loss, wo_agent')
    parser.add_argument('-r', '--resume', action='store_true', default=False, required=False,
                        help='whether resume from the last training process')
    parser.add_argument('-ne', '--n_epoch', type=int, default=300, required=False,
                        help='number of training epoches')
    parser.add_argument('-s', '--save_cycle', type=int, default=0, required=False,
                        help='save weight every s epoches')
    parser.add_argument('-n', '--network', type=str, nargs='+', default=['profen', 'affine'], required=False,
                        help='train which network, choices: affine, profen')
    args = parser.parse_args()
    
    os.environ['CUDA_VISIBLE_DEVICES'] = str(args.gpu)
    if 'affine' in args.network:
        Affine2DTrainer(fold=-1, save_cycle=args.save_cycle, n_epoch=args.n_epoch).train(resume=args.resume)
    if 'profen' in args.network:
        for abl in args.ablations:
            for fold in args.folds:
                ProfenTrainer(ablation=abl if abl != 'none' else None, fold=fold, n_folds=args.n_folds, 
                            save_cycle=args.save_cycle, n_epoch=args.n_epoch).train(resume=args.resume)
