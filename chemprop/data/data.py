from argparse import Namespace
from collections import defaultdict
from multiprocessing import Pool
from logging import Logger
import random
import math
from typing import Dict, List, Tuple, Union, Set, FrozenSet
from copy import deepcopy

import numpy as np
from torch.utils.data.dataset import Dataset
from rdkit import Chem

from .scaler import StandardScaler
from .vocab import load_vocab, Vocab, get_substructures, substructure_to_feature
from chemprop.features import morgan_fingerprint, rdkit_2d_features


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
        if args is not None:
            features_generator, predict_features, sparse = args.features_generator, args.predict_features, args.sparse
            self.bert_pretraining = args.dataset_type == 'bert_pretraining'
            self.bert_mask_prob = args.bert_mask_prob
            self.bert_mask_type = args.bert_mask_type
            self.bert_vocab_func = args.bert_vocab_func
            self.substructure_sizes = args.bert_substructure_sizes
            self.args = args
        else:
            features_generator = None
            predict_features = sparse = self.bert_pretraining = False

        if features is not None and features_generator is not None:
            raise ValueError('Currently cannot provide both loaded features and a features generator.')

        if use_compound_names:
            self.compound_name = line[0]  # str
            line = line[1:]
        else:
            self.compound_name = None

        self.smiles = line[0]  # str

        if features is not None:
            if len(features.shape) > 1:
                features = np.squeeze(features)
        self.features = features

        # Generate additional features if given a generator
        if features_generator is not None:
            self.features = []
            for fg in features_generator:
                if fg == 'morgan':
                    self.features.extend(morgan_fingerprint(self.smiles))  # np.ndarray
                elif fg == 'morgan_count':
                    self.features.extend(morgan_fingerprint(self.smiles, use_counts=True))
                elif fg == 'rdkit_2d':
                    self.features.extend(rdkit_2d_features(self.smiles, args))
                else:
                    raise ValueError('features_generator type "{}" not supported.'.format(fg))
            self.features = np.array(self.features)

        if args is not None and args.dataset_type in ['unsupervised', 'bert_pretraining']:
            self.num_tasks = 1  # TODO could try doing "multitask" with multiple different clusters?
            self.targets = None
        else:
            if predict_features:
                self.targets = self.features  # List[float]
            else:
                self.targets = [float(x) if x != '' else None for x in line[1:]]  # List[Optional[float]]

            self.num_tasks = len(self.targets)  # int

            if sparse:
                self.targets = SparseNoneArray(self.targets)
    
    def bert_init(self, args: Namespace):
        if not self.bert_pretraining:
            raise Exception('Should not do this unless using bert_pretraining.')

        self.vocab_targets, self.nb_indices = args.vocab.smiles2indices(self.smiles)
        self.recreate_mask()

    def recreate_mask(self):
        # Note: 0s to mask atoms which should be predicted

        if not self.bert_pretraining:
            raise Exception('Cannot recreate mask without bert_pretraining on.')
        
        if self.bert_vocab_func == 'substructure':
            mol = Chem.MolFromSmiles(self.smiles)
            self.substructures = get_substructures(list(mol.GetAtoms()), 
                                                   sizes=self.substructure_sizes, 
                                                   max_count=1)  # TODO could change max_count
            self.substructure_index_map = substructure_index_mapping(self.smiles, self.substructures)
            self.mask = np.ones(max(self.substructure_index_map) + 1)
            self.mask[-len(self.substructures):] = 0  # the last entries correspond to the substructures
            self.mask = list(self.mask)

            sorted_substructures = sorted(list(self.substructures), key=lambda x: self.substructure_index_map[list(x)[0]])
            substructure_index_labels = \
                    [self.args.vocab.w2i(substructure_to_feature(mol, substruct)) for substruct in sorted_substructures]
            self.vocab_targets = np.zeros(len(self.mask))  # these should never get used
            if len(substructure_index_labels) > 0:  # it's possible to find none at all in e.g. a 2-atom molecule
                self.vocab_targets[-len(self.substructures):] = np.array(substructure_index_labels)
            return

        num_targets = len(self.vocab_targets)
        if num_targets == 0:
            # Somehow this happens on very rare occasions, where smiles = ''; just do this to avoid a crash.
            # In theory we would just omit this data point, but I don't want to risk
            # screwing up the alignment with any of the files containing pre-saved features,
            # which could be a very nasty bug to catch later on. 
            self.mask = []
            return

        if self.bert_mask_type == 'cluster':
            self.mask = np.ones(num_targets)
            atoms = set(range(num_targets))
            while len(atoms) != 0:
                atom = atoms.pop()
                neighbors = self.nb_indices[atom]
                cluster = [atom] + neighbors

                # note: divide by cluster size to preserve overall probability of masking each atom
                if np.random.random() < self.bert_mask_prob / len(cluster):
                    self.mask[cluster] = 0
                    atoms -= set(neighbors)

            # Ensure at least one cluster of 0s
            if sum(self.mask) == len(self.mask):
                atom = np.random.randint(len(self.mask))
                neighbors = self.nb_indices[atom]
                cluster = [atom] + neighbors
                self.mask[cluster] = 0

        elif self.bert_mask_type == 'correlation':
            self.mask = np.random.rand(num_targets) > self.bert_mask_prob  # len = num_atoms

            # randomly change parts of mask to increase correlation between neighbors
            for _ in range(len(self.mask)):  # arbitrary num iterations; could set in parsing if we want
                index_to_change = random.randint(0, len(self.mask) - 1)
                if len(self.nb_indices[index_to_change]) > 0:  # can be 0 for single heavy atom molecules
                    nbr_index = random.randint(0, len(self.nb_indices[index_to_change]) - 1)
                    self.mask[index_to_change] = self.mask[nbr_index]

            # Ensure at least one 0 so at least one thing is predicted
            if sum(self.mask) == len(self.mask):
                self.mask[np.random.randint(len(self.mask))] = 0

        elif self.bert_mask_type == 'random':
            self.mask = np.random.rand(num_targets) > self.bert_mask_prob  # len = num_atoms

            # Ensure at least one 0 so at least one thing is predicted
            if sum(self.mask) == len(self.mask):
                self.mask[np.random.randint(len(self.mask))] = 0

        else:
            raise ValueError('bert_mask_type "{}" not supported.'.format(self.bert_mask_type))

        # np.ndarray --> list
        self.mask = list(self.mask)

    def set_targets(self, targets):  # for unsupervised pretraining only
        self.targets = targets


class MoleculeDataset(Dataset):
    def __init__(self, data: List[MoleculeDatapoint]):
        self.data = data
        self.bert_pretraining = self.data[0].bert_pretraining if len(self.data) > 0 else False
        self.bert_vocab_func = self.data[0].bert_vocab_func if len(self.data) > 0 else None
        self.features_size = len(self.data[0].features) if len(self.data) > 0 and self.data[0].features is not None else None
        self.scaler = None
    
    def bert_init(self, args: Namespace, logger: Logger = None):
        debug = logger.debug if logger is not None else print

        if not hasattr(args, 'vocab'):
            debug('Determining vocab')
            args.vocab = load_vocab(args.checkpoint_paths[0]) if args.checkpoint_paths is not None else Vocab(args, self.smiles())
            debug('Vocab/Output size = {:,}'.format(args.vocab.output_size))

        if not hasattr(args, 'features_size') or args.features_size is None:
            args.features_size = self.features_size

        if args.sequential:
            for d in self.data:
                d.bert_init(args)
        else:
            try:
                # reassign self.data since the pool seems to deepcopy the data before calling bert_init
                self.data = Pool().map(parallel_bert_init, [(d, deepcopy(args)) for d in self.data])
            except OSError:  # apparently it's possible to get an OSError about too many open files here...?
                for d in self.data:
                    d.bert_init(args)

        debug('Finished initializing targets and masks for bert')

    def compound_names(self) -> List[str]:
        if len(self.data) == 0 or self.data[0].compound_name is None:
            return None

        return [d.compound_name for d in self.data]

    def smiles(self) -> List[str]:
        if hasattr(self.data[0], 'substructures'):
            return [(d.smiles, d.substructure_index_map, d.substructures) for d in self.data]
        return [d.smiles for d in self.data]

    def features(self) -> List[np.ndarray]:
        if len(self.data) == 0 or self.data[0].features is None:
            return None

        return [d.features for d in self.data]

    def targets(self) -> Union[List[List[float]],
                               List[SparseNoneArray],
                               List[int],
                               Dict[str, Union[List[np.ndarray], List[int]]]]:
        if self.bert_pretraining:
            return {
                'features': self.features(),
                'vocab': [word for d in self.data for word in d.vocab_targets]
            }

        return [d.targets for d in self.data]

    def num_tasks(self) -> int:
        return self.data[0].num_tasks if len(self.data) > 0 else None

    def mask(self) -> List[int]:
        if not self.bert_pretraining:
            raise Exception('Mask is undefined without bert_pretraining on.')

        return [m for d in self.data for m in d.mask]

    def shuffle(self, seed: int = None):
        if seed is not None:
            random.seed(seed)

        random.shuffle(self.data)

        if self.bert_pretraining:
            for d in self.data:
                d.recreate_mask()

    def chunk(self, num_chunks: int, seed: int = None) -> List['MoleculeDataset']:
        self.shuffle(seed)
        datasets = []
        chunk_len = math.ceil(len(self.data) / num_chunks)
        for i in range(num_chunks):
            datasets.append(MoleculeDataset(self.data[i * chunk_len:(i + 1) * chunk_len]))

        return datasets
    
    def normalize_features(self, scaler: StandardScaler = None) -> StandardScaler:
        if len(self.data) == 0 or self.data[0].features is None:
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
            d.features = scaler.transform(d.features.reshape(1, -1))[0]

        return scaler
    
    def set_targets(self, targets: List[float]):  # for unsupervised pretraining only
        assert len(self.data) == len(targets) # assume user kept them aligned
        for i in range(len(self.data)):
            self.data[i].set_targets(targets[i])

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, item) -> MoleculeDatapoint:
        return self.data[item]


def parallel_bert_init(pair: Tuple[MoleculeDatapoint, Namespace]) -> MoleculeDatapoint:
    """
    Runs bert_init on a MoleculeDatapoint.

    :param pair: A tuple of a molecule datapoint and arguments.
    :return: The molecule datapoint after having run bert_init.
    """
    d, args = pair
    d.bert_init(args)

    return d


def substructure_index_mapping(smiles: str, substructures: Set[FrozenSet[int]]) -> List[int]:
    """
    Return a deterministic mapping of indices from the original molecule atoms to the
    molecule with some substructures collapsed.

    :param smiles: smiles string
    :param substructures: indices of atoms in substructures
    """
    num_atoms = Chem.MolFromSmiles(smiles).GetNumAtoms()
    atoms_in_substructures = set().union(*substructures)  # set of all indices of atoms in a substructure
    remaining_atoms = set(range(num_atoms)).difference(atoms_in_substructures)

    # collapsed substructures shouldn't share atoms
    assert sum([len(s) for s in substructures]) == len(atoms_in_substructures)

    substructures = list(substructures)
    substructures = [sorted(list(substruct)) for substruct in substructures]
    substructures = sorted(substructures, key=lambda x: x[0])  # should give unique ordering

    index_map = [None for _ in range(num_atoms)]
    remaining_atom_ordering = sorted(list(remaining_atoms))
    for i in range(len(remaining_atom_ordering)):
        index_map[remaining_atom_ordering[i]] = i
    for i in range(len(substructures)):
        for atom in substructures[i]:
            index_map[atom] = len(remaining_atom_ordering) + i
    
    return index_map
