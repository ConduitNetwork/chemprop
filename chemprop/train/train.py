from argparse import Namespace
import logging
import random
from typing import Callable, List, Union
import os

from tensorboardX import SummaryWriter
import torch
import torch.nn as nn
from torch.nn.utils.clip_grad import clip_grad_norm_
from torch.optim import Optimizer
from torch.optim.lr_scheduler import _LRScheduler, ExponentialLR
from tqdm import trange, tqdm
import pickle

from chemprop.data import MoleculeDataset
from chemprop.features import featurization, mol2graph
from chemprop.nn_utils import compute_gnorm, compute_pnorm, NoamLR


def train(model: nn.Module,
          data: Union[MoleculeDataset, List[MoleculeDataset]],
          loss_func: Callable,
          optimizer: Optimizer,
          scheduler: _LRScheduler,
          args: Namespace,
          n_iter: int = 0,
          logger: logging.Logger = None,
          writer: SummaryWriter = None,
          chunk_names: bool = False,
          val_smiles: List[str] = None,
          test_smiles: List[str] = None) -> int:
    """
    Trains a model for an epoch.

    :param model: Model.
    :param data: A MoleculeDataset (or a list of MoleculeDatasets if using moe).
    :param loss_func: Loss function.
    :param optimizer: An Optimizer.
    :param scheduler: A learning rate scheduler.
    :param args: Arguments.
    :param n_iter: The number of iterations (training examples) trained on so far.
    :param logger: A logger for printing intermediate results.
    :param writer: A tensorboardX SummaryWriter.
    :param chunk_names: Whether to train on the data in chunks. In this case,
    data must be a list of paths to the data chunks.
    :param val_smiles: Validation smiles strings without targets.
    :param test_smiles: Test smiles strings without targets, used for adversarial setting.
    :return: The total number of iterations (training examples) trained on so far.
    """
    debug = logger.debug if logger is not None else print
    
    model.train()

    if args.dataset_type == 'bert_pretraining':
        features_loss = nn.MSELoss()

    if chunk_names:
        for path, memo_path in tqdm(data, total=len(data)):
            featurization.SMILES_TO_FEATURES = dict()
            if os.path.isfile(memo_path):
                found_memo = True
                with open(memo_path, 'rb') as f:
                    featurization.SMILES_TO_FEATURES = pickle.load(f)
            else:
                found_memo = False
            with open(path, 'rb') as f:
                chunk = pickle.load(f)
            if args.moe:
                for source in chunk:
                    source.shuffle()
            else:
                chunk.shuffle()
            n_iter = train(
                model=model,
                data=chunk,
                loss_func=loss_func,
                optimizer=optimizer,
                scheduler=scheduler,
                args=args,
                n_iter=n_iter,
                logger=logger,
                writer=writer,
                chunk_names=False,
                val_smiles=val_smiles,
                test_smiles=test_smiles
            )
            if not found_memo:
                with open(memo_path, 'wb') as f:
                    pickle.dump(featurization.SMILES_TO_GRAPH, f, protocol=pickle.HIGHEST_PROTOCOL)
        return n_iter
    
    if not args.moe:
        data.shuffle()

    loss_sum, iter_count = 0, 0
    if args.adversarial:
        if args.moe:
            train_smiles = []
            for d in data:
                train_smiles += d.smiles()
        else:
            train_smiles = data.smiles()
        train_val_smiles = train_smiles + val_smiles
        d_loss_sum, g_loss_sum, gp_norm_sum = 0, 0, 0

    if args.moe:
        test_smiles = list(test_smiles)
        random.shuffle(test_smiles)
        train_smiles = []
        for d in data:
            d.shuffle()
            train_smiles.append(d.smiles())
        num_iters = min(len(test_smiles), min([len(d) for d in data]))
    else:
        num_iters = len(data) if args.last_batch else len(data) // args.batch_size * args.batch_size

    for i in trange(0, num_iters, args.batch_size):
        if args.moe:
            if not args.batch_domain_encs:
                model.compute_domain_encs(train_smiles)  # want to recompute every batch
            batch = [MoleculeDataset(d[i:i + args.batch_size]) for d in data]
            train_batch, train_targets = [], []
            for b in batch:
                tb, tt = b.smiles(), b.targets()
                train_batch.append(tb)
                train_targets.append(tt)
            test_batch = test_smiles[i:i + args.batch_size]
            loss = model.compute_loss(train_batch, train_targets, test_batch)
            model.zero_grad()

            loss_sum += loss.item()
            iter_count += len(batch)
        else:
            # Prepare batch
            if not args.last_batch and i + args.batch_size > len(data):
                break
            mol_batch = MoleculeDataset(data[i:i + args.batch_size])
            smiles_batch, features_batch, target_batch = mol_batch.smiles(), mol_batch.features(), mol_batch.targets()

            if args.dataset_type == 'bert_pretraining':
                batch = mol2graph(smiles_batch, args)
                mask = mol_batch.mask()
                batch.bert_mask(mask)
                mask = 1 - torch.FloatTensor(mask)  # num_atoms
                features_targets = torch.FloatTensor(target_batch['features']) if target_batch['features'] is not None else None  # num_molecules x features_size
                targets = torch.FloatTensor(target_batch['vocab'])  # num_atoms
                if args.bert_vocab_func == 'feature_vector':
                    mask = mask.reshape(-1, 1)
                else:
                    targets = targets.long()
            else:
                batch = smiles_batch
                mask = torch.Tensor([[x is not None for x in tb] for tb in target_batch])
                targets = torch.Tensor([[0 if x is None else x for x in tb] for tb in target_batch])

            if next(model.parameters()).is_cuda:
                mask, targets = mask.cuda(), targets.cuda()

                if args.dataset_type == 'bert_pretraining' and features_targets is not None:
                    features_targets = features_targets.cuda()

            # Run model
            model.zero_grad()
            preds = model(batch, features_batch)
            if args.dataset_type == 'regression_with_binning':
                preds = preds.view(targets.size(0), targets.size(1), -1)
                targets = targets.long()
                loss = 0
                for task in range(targets.size(1)):
                    loss += loss_func(preds[:, task, :], targets[:, task]) * mask[:, task]  # for some reason cross entropy doesn't support multi target
                loss = loss.sum() / mask.sum()
            else:
                if args.dataset_type == 'unsupervised':
                    targets = targets.long().reshape(-1)

                if args.dataset_type == 'bert_pretraining':
                    features_preds, preds = preds['features'], preds['vocab']
                
                if args.dataset_type == 'kernel':
                    preds = preds.view(int(preds.size(0)/2), 2, preds.size(1))
                    preds = model.kernel_output_layer(preds)

                loss = loss_func(preds, targets) * mask
                if args.predict_features_and_task:
                    loss = (loss.sum() + loss[:, :-args.features_size].sum() * (args.task_weight-1)) \
                                / (mask.sum() + mask[:, :-args.features_size].sum() * (args.task_weight-1))
                else:
                    loss = loss.sum() / mask.sum()

                if args.dataset_type == 'bert_pretraining' and features_targets is not None:
                    loss += features_loss(features_preds, features_targets)

            loss_sum += loss.item()
            iter_count += len(mol_batch)

        loss.backward()

        if args.max_grad_norm is not None:
            clip_grad_norm_(model.parameters(), args.max_grad_norm)

        optimizer.step()

        if args.adjust_weight_decay:
            current_pnorm = compute_pnorm(model)
            if current_pnorm < args.pnorm_target:
                for i in range(len(optimizer.param_groups)):
                    optimizer.param_groups[i]['weight_decay'] = max(0, optimizer.param_groups[i]['weight_decay'] - args.adjust_weight_decay_step)
            else:
                for i in range(len(optimizer.param_groups)):
                    optimizer.param_groups[i]['weight_decay'] += args.adjust_weight_decay_step

        if isinstance(scheduler, NoamLR):
            scheduler.step()

        if args.adversarial:
            for _ in range(args.gan_d_per_g):
                train_val_smiles_batch = random.sample(train_val_smiles, args.batch_size)
                test_smiles_batch = random.sample(test_smiles, args.batch_size)
                d_loss, gp_norm = model.train_D(train_val_smiles_batch, test_smiles_batch)
            train_val_smiles_batch = random.sample(train_val_smiles, args.batch_size)
            test_smiles_batch = random.sample(test_smiles, args.batch_size)
            g_loss = model.train_G(train_val_smiles_batch, test_smiles_batch)

            # we probably only care about the g_loss honestly
            d_loss_sum += d_loss * args.batch_size
            gp_norm_sum += gp_norm * args.batch_size
            g_loss_sum += g_loss * args.batch_size

        n_iter += len(mol_batch)

        # Log and/or add to tensorboard
        if (n_iter // args.batch_size) % args.log_frequency == 0:
            lrs = scheduler.get_lr()
            pnorm = compute_pnorm(model)
            gnorm = compute_gnorm(model)
            loss_avg = loss_sum / iter_count
            if args.adversarial:
                d_loss_avg, g_loss_avg, gp_norm_avg = d_loss_sum / iter_count, g_loss_sum / iter_count, gp_norm_sum / iter_count
                d_loss_sum, g_loss_sum, gp_norm_sum = 0, 0, 0
            loss_sum, iter_count = 0, 0

            lrs_str = ', '.join('lr_{} = {:.4e}'.format(i, lr) for i, lr in enumerate(lrs))
            debug("Loss = {:.4e}, PNorm = {:.4f}, GNorm = {:.4f}, {}".format(loss_avg, pnorm, gnorm, lrs_str))
            if args.adversarial:
                debug("D Loss = {:.4e}, G Loss = {:.4e}, GP Norm = {:.4}".format(d_loss_avg, g_loss_avg, gp_norm_avg))

            if writer is not None:
                writer.add_scalar('train_loss', loss_avg, n_iter)
                writer.add_scalar('param_norm', pnorm, n_iter)
                writer.add_scalar('gradient_norm', gnorm, n_iter)
                for i, lr in enumerate(lrs):
                    writer.add_scalar('learning_rate_{}'.format(i), lr, n_iter)

    return n_iter
