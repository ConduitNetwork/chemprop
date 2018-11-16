from argparse import Namespace
from collections import defaultdict
import random
import math
from typing import List

import numpy as np
from rdkit import Chem
import torch
from torch.utils.data.dataset import Dataset

from .scaler import StandardScaler
from chemprop.features import atom_features, morgan_fingerprint, rdkit_2d_features


class SparseNoneArray:
    def __init__(self, targets: List[float]):
        self.length = len(targets)
        self.targets = defaultdict(lambda: None, {i: x for i, x in enumerate(targets) if x is not None})
    
    def __len__(self):
        return self.length
    
    def __getitem__(self, i):
        if i >= self.length:
            raise IndexError
        return self.targets[i]


class MoleculeDatapoint:
    def __init__(self,
                 line: List[str],
                 args: Namespace,
                 features: np.ndarray = None,
                 use_compound_names: bool = False):
        """
        Initializes a MoleculeDatapoint.

        :param line: A list of strings generated by separating a line in a data CSV file by comma.
        :param args: Argument Namespace.
        :param features: A numpy array containing additional features (ex. Morgan fingerprint).
        :param use_compound_names: Whether the data CSV includes the compound name on each line.
        """
        features_generator = args.features_generator if args is not None else None
        predict_features = args.predict_features if args is not None else False
        sparse = args.sparse if args is not None else False
        self.bert_pretraining = args.dataset_type == 'bert_pretraining' if args is not None else False

        if features is not None and features_generator is not None:
            raise ValueError('Currently cannot provide both loaded features and a features generator.')

        if use_compound_names:
            self.compound_name = line[0]  # str
            line = line[1:]
        else:
            self.compound_name = None

        self.smiles = line[0]  # str
        self.features = features  # np.ndarray
        if self.features is not None and len(self.features.shape) > 1:
            self.features = np.squeeze(self.features)

        # Generate additional features if given a generator
        if features_generator is not None:
            self.features = []
            for fg in features_generator:
                if fg == 'morgan':
                    self.features.append(morgan_fingerprint(self.smiles))  # np.ndarray
                elif fg == 'morgan_count':
                    self.features.append(morgan_fingerprint(self.smiles, use_counts=True))
                elif fg == 'rdkit_2d':
                    self.features.append(rdkit_2d_features(self.smiles))
                else:
                    raise ValueError('features_generator type "{}" not supported.'.format(fg))
            self.features = np.concatenate(self.features)
        
        if args is not None and args.dataset_type == 'unsupervised':
            self.num_tasks = 1  # TODO could try doing "multitask" with multiple different clusters?
            self.targets = [None]
        else:
            if predict_features:
                self.targets = self.features.tolist()  # List[float]
            else:
                self.targets = [float(x) if x != '' else None for x in line[1:]]  # List[Optional[float]]

            self.num_tasks = len(self.targets)  # int

            if sparse:
                self.targets = SparseNoneArray(self.targets)
    
    def bert_init(self, args: Namespace):
        if not self.bert_pretraining:
            raise Exception('Should not do this unless using bert_pretraining.')
        self.mask_prob = 0.15
        atoms = Chem.MolFromSmiles(self.smiles).GetAtoms()
        self.n_atoms = len(atoms)
        self.targets = torch.LongTensor([args.vocab_mapping[str(atom_features(atom))] for atom in atoms])
        self.recreate_mask()

    def recreate_mask(self):
        if not self.bert_pretraining:
            raise Exception('Cannot recreate mask without bert_pretraining on.')

        self.mask = (torch.rand(self.n_atoms) > self.mask_prob).float()  # num_atoms  (0s to mask atoms)

    def set_targets(self, targets):  # for unsupervised pretraining only
        self.targets = targets


class MoleculeDataset(Dataset):
    def __init__(self, data: List[MoleculeDatapoint]):
        self.data = data
        self.bert_pretraining = self.data[0].bert_pretraining
        self.scaler = None
    
    def bert_init(self, args: Namespace):
        for d in self.data:
            d.bert_init(args)

    def compound_names(self) -> List[str]:
        if self.data[0].compound_name is None:
            return None

        return [d.compound_name for d in self.data]

    def smiles(self) -> List[str]:
        return [d.smiles for d in self.data]

    def features(self) -> List[np.ndarray]:
        if self.data[0].features is None:
            return None

        return [d.features for d in self.data]

    def targets(self) -> List[float]:
        return [d.targets for d in self.data]

    def num_tasks(self) -> int:
        return self.data[0].num_tasks

    def mask(self) -> torch.FloatTensor:
        if not self.bert_pretraining:
            raise Exception('Mask is undefined without bert_pretraining on.')

        return torch.cat([torch.zeros((1))] + [d.mask for d in self.data], dim=0)  # note the first entry is padding

    def shuffle(self, seed: int = None):
        if seed is not None:
            random.seed(seed)

        random.shuffle(self.data)

        if self.bert_pretraining:
            for d in self.data:
                d.recreate_mask()

    def chunk(self, num_chunks: int, seed: int = None):
        self.shuffle(seed)
        datasets = []
        chunk_len = math.ceil(len(self.data) / num_chunks)
        for i in range(num_chunks):
            datasets.append(MoleculeDataset(self.data[i * chunk_len:(i + 1) * chunk_len]))
        return datasets
    
    def normalize_features(self, scaler: StandardScaler = None) -> StandardScaler:
        if self.data[0].features is None:
            return None

        if scaler is not None:
            self.scaler = scaler
        else:
            if self.scaler is not None:
                scaler = self.scaler
            else:
                features = np.vstack([d.features for d in self.data])
                scaler = StandardScaler(replace_nan_token=0)
                scaler.fit(features)
                self.scaler = scaler

        for d in self.data:
            d.features = scaler.transform(d.features.reshape(1, -1))
        return scaler
    
    def set_targets(self, targets: List[float]):  # for unsupervised pretraining only
        assert len(self.data) == len(targets) # assume user kept them aligned
        for i in range(len(self.data)):
            self.data[i].set_targets(targets[i])

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, item) -> MoleculeDatapoint:
        return self.data[item]
