#!/usr/bin/env python3.7

## in WDL: ${task1_script} ${token_outprefix} ${loom_inpath} ${gene_median_dictionary_pkl} ${token_dictionary_pkl}
## 0th argument = task1 script (i.e. path to this script)
## first argument = sys.argv[1] (token_outprefix)
## second argument = sys.argv[2] (loom_inpath)
## third argument = sys.argv[3] (gene_median_dictionary_pkl)
## fourth argument = sys.argv[4] (token_dictionary_pkl)
## fifth argument = sys.argv[5] (output_dir)


from __future__ import annotations

###############################
# define modified tokenizer fns
###############################

print("Defining tokenizer functions")
#from typing import Literal
try:
    from typing import Literal
except ImportError:
    from typing_extensions import Literal

import pickle
from pathlib import Path

import logging

import warnings
warnings.filterwarnings("ignore", message=".*The 'nopython' keyword.*")

import anndata as ad
import loompy as lp
import numpy as np
import scipy.sparse as sp
from datasets import Dataset
import sys # sys must be imported for use of sys.argv below

logger = logging.getLogger(__name__)

# get .pkl files
GENE_MEDIAN_FILE = sys.argv[3]
TOKEN_DICTIONARY_FILE = sys.argv[4]

def rank_genes(gene_vector, gene_tokens):
    """
    Rank gene expression vector.
    """
    # sort by median-scaled gene values
    sorted_indices = np.argsort(-gene_vector)
    return gene_tokens[sorted_indices]


def tokenize_cell(gene_vector, gene_tokens):
    """
    Convert normalized gene expression vector to tokenized rank value encoding.
    """
    # create array of gene vector with token indices
    # mask undetected genes
    nonzero_mask = np.nonzero(gene_vector)[0]
    # rank by median-scaled gene values
    return rank_genes(gene_vector[nonzero_mask], gene_tokens[nonzero_mask])


class TranscriptomeTokenizer:
    def __init__(
        self,
        custom_attr_name_dict=None,
        nproc=1,
        gene_median_file=GENE_MEDIAN_FILE,
        token_dictionary_file=TOKEN_DICTIONARY_FILE,
    ):
        """
        Initialize tokenizer.
        Parameters
        ----------
        custom_attr_name_dict : None, dict
            Dictionary of custom attributes to be added to the dataset.
            Keys are the names of the attributes in the loom file.
            Values are the names of the attributes in the dataset.
        nproc : int
            Number of processes to use for dataset mapping.
        gene_median_file : Path
            Path to pickle file containing dictionary of non-zero median
            gene expression values across Genecorpus-30M.
        token_dictionary_file : Path
            Path to pickle file containing token dictionary (Ensembl IDs:token).
        """
        # dictionary of custom attributes {output dataset column name: input .loom column name}
        self.custom_attr_name_dict = custom_attr_name_dict

        # number of processes for dataset mapping
        self.nproc = nproc

        # load dictionary of gene normalization factors
        # (non-zero median value of expression across Genecorpus-30M)
        with open(gene_median_file, "rb") as f:
            self.gene_median_dict = pickle.load(f)

        # load token dictionary (Ensembl IDs:token)
        with open(token_dictionary_file, "rb") as f:
            self.gene_token_dict = pickle.load(f)

        # gene keys for full vocabulary
        self.gene_keys = list(self.gene_median_dict.keys())

        # protein-coding and miRNA gene list dictionary for selecting .loom rows for tokenization
        self.genelist_dict = dict(zip(self.gene_keys, [True] * len(self.gene_keys)))

    def tokenize_data(
        self,
        data_directory: Path | str,
        output_directory: Path | str,
        output_prefix: str,
        file_format: Literal["loom", "h5ad"] = "loom",
        use_generator: bool = False,
    ):
        """
        Tokenize .loom files in loom_data_directory and save as tokenized .dataset in output_directory.
        Parameters
        ----------
        loom_data_directory : Path
            Path to directory containing loom files or anndata files
        output_directory : Path
            Path to directory where tokenized data will be saved as .dataset
        output_prefix : str
            Prefix for output .dataset
        file_format : str
            Format of input files. Can be "loom" or "h5ad".
        use_generator : bool
            Whether to use generator or dict for tokenization.
        """
        tokenized_cells, cell_metadata = self.tokenize_files(
            Path(data_directory), file_format
        )
        tokenized_dataset = self.create_dataset(tokenized_cells, cell_metadata, use_generator=use_generator)

           
        #output_path = (Path(output_directory) / output_prefix).with_suffix(".dataset")
        output_path = (Path(output_directory) / output_prefix)
        print("Inside tokeniser function ::: Saving file to...", output_path) 
        tokenized_dataset.save_to_disk(output_path)

    def tokenize_files(
        self, data_directory, file_format: Literal["loom", "h5ad"] = "loom"
    ):
        tokenized_cells = []
        if self.custom_attr_name_dict is not None:
            cell_attr = [attr_key for attr_key in self.custom_attr_name_dict.keys()]
            cell_metadata = {attr_key: [] for attr_key in self.custom_attr_name_dict.values()}

        tokenize_file_fn = (
            self.tokenize_loom if file_format == "loom" else self.tokenize_anndata
        )

        print(f"Tokenizing {data_directory}")
        file_tokenized_cells, file_cell_metadata = tokenize_file_fn(data_directory)

        if self.custom_attr_name_dict is not None:
            for k in cell_attr:
                cell_metadata[self.custom_attr_name_dict[k]] += file_cell_metadata[k]
        else:
            cell_metadata = None

        ## remove loop that searches for .loom files 
        ## loops through directories to tokenize .loom files
        #file_found = 0
        ## loops through directories to tokenize .loom or .h5ad files
        #tokenize_file_fn = (
        #    self.tokenize_loom if file_format == "loom" else self.tokenize_anndata
        #)
        #for file_path in data_directory.glob("*.{}".format(file_format)):
        #    file_found = 1
        #    print(f"Tokenizing {file_path}")
        #    file_tokenized_cells, file_cell_metadata = tokenize_file_fn(file_path)
        #    tokenized_cells += file_tokenized_cells
        #    if self.custom_attr_name_dict is not None:
        #        for k in cell_attr:
        #            cell_metadata[self.custom_attr_name_dict[k]] += file_cell_metadata[k]
        #    else:
        #        cell_metadata = None
        #
        #if file_found == 0:
        #    logger.error(
        #        f"No .{file_format} files found in directory {data_directory}.")
        #    raise

        return tokenized_cells, cell_metadata

    def tokenize_anndata(self, adata_file_path, target_sum=10_000, chunk_size=512):
        adata = ad.read(adata_file_path, backed="r")

        if self.custom_attr_name_dict is not None:
            file_cell_metadata = {
                attr_key: [] for attr_key in self.custom_attr_name_dict.keys()
            }

        coding_miRNA_loc = np.where(
            [self.genelist_dict.get(i, False) for i in adata.var["ensembl_id"]]
        )[0]
        norm_factor_vector = np.array(
            [
                self.gene_median_dict[i]
                for i in adata.var["ensembl_id"][coding_miRNA_loc]
            ]
        )
        coding_miRNA_ids = adata.var["ensembl_id"][coding_miRNA_loc]
        coding_miRNA_tokens = np.array(
            [self.gene_token_dict[i] for i in coding_miRNA_ids]
        )

        try:
            _ = adata.obs["filter_pass"]
        except KeyError:
            var_exists = False
        else:
            var_exists = True

        if var_exists:
            filter_pass_loc = np.where(
                [i == 1 for i in adata.obs["filter_pass"]]
            )[0]
        elif not var_exists:
            print(
                f"{adata_file_path} has no column attribute 'filter_pass'; tokenizing all cells."
            )
            filter_pass_loc = np.array([i for i in range(adata.shape[0])])

        tokenized_cells = []

        for i in range(0, len(filter_pass_loc), chunk_size):
            idx = filter_pass_loc[i:i+chunk_size]

            n_counts = adata[idx].obs['n_counts'].values[:, None]
            X_view = adata[idx, coding_miRNA_loc].X
            X_norm = (X_view / n_counts * target_sum / norm_factor_vector)
            X_norm = sp.csr_matrix(X_norm)

            tokenized_cells += [
                rank_genes(X_norm[i].data, coding_miRNA_tokens[X_norm[i].indices])
                for i in range(X_norm.shape[0])
            ]

            # add custom attributes for subview to dict
            if self.custom_attr_name_dict is not None:
                for k in file_cell_metadata.keys():
                    file_cell_metadata[k] += adata[idx].obs[k].tolist()
            else:
                file_cell_metadata = None

        return tokenized_cells, file_cell_metadata

    def tokenize_loom(self, loom_file_path, target_sum=10_000):
        if self.custom_attr_name_dict is not None:
            file_cell_metadata = {
                attr_key: [] for attr_key in self.custom_attr_name_dict.keys()
            }

        with lp.connect(str(loom_file_path)) as data:
            # define coordinates of detected protein-coding or miRNA genes and vector of their normalization factors
            coding_miRNA_loc = np.where(
                [self.genelist_dict.get(i, False) for i in data.ra["ensembl_id"]]
            )[0]
            norm_factor_vector = np.array(
                [
                    self.gene_median_dict[i]
                    for i in data.ra["ensembl_id"][coding_miRNA_loc]
                ]
            )
            coding_miRNA_ids = data.ra["ensembl_id"][coding_miRNA_loc]
            coding_miRNA_tokens = np.array(
                [self.gene_token_dict[i] for i in coding_miRNA_ids]
            )

            # define coordinates of cells passing filters for inclusion (e.g. QC)
            try:
                data.ca["filter_pass"]
            except AttributeError:
                var_exists = False
            else:
                var_exists = True

            if var_exists:
                filter_pass_loc = np.where(
                    [i == 1 for i in data.ca["filter_pass"]]
                )[0]
            elif not var_exists:
                print(
                    f"{loom_file_path} has no column attribute 'filter_pass'; tokenizing all cells."
                )
                filter_pass_loc = np.array([i for i in range(data.shape[1])])

            # scan through .loom files and tokenize cells
            tokenized_cells = []
            for (_ix, _selection, view) in data.scan(items=filter_pass_loc, axis=1):
                # select subview with protein-coding and miRNA genes
                subview = view.view[coding_miRNA_loc, :]

                # normalize by total counts per cell and multiply by 10,000 to allocate bits to precision
                # and normalize by gene normalization factors
                subview_norm_array = (
                    subview[:, :]
                    / subview.ca.n_counts
                    * target_sum
                    / norm_factor_vector[:, None]
                )
                # tokenize subview gene vectors
                tokenized_cells += [
                    tokenize_cell(subview_norm_array[:, i], coding_miRNA_tokens)
                    for i in range(subview_norm_array.shape[1])
                ]

                # add custom attributes for subview to dict
                if self.custom_attr_name_dict is not None:
                    for k in file_cell_metadata.keys():
                        file_cell_metadata[k] += subview.ca[k].tolist()
                else:
                    file_cell_metadata = None

        return tokenized_cells, file_cell_metadata

    def create_dataset(self, tokenized_cells, cell_metadata, use_generator=False, keep_uncropped_input_ids=False):
        print("Creating dataset.")
        # create dict for dataset creation
        dataset_dict = {"input_ids": tokenized_cells}
        if self.custom_attr_name_dict is not None:
            dataset_dict.update(cell_metadata)

        # create dataset
        if use_generator:
            def dict_generator():
                for i in range(len(tokenized_cells)):
                    yield {k: dataset_dict[k][i] for k in dataset_dict.keys()}
            output_dataset = Dataset.from_generator(dict_generator, num_proc=self.nproc)
        else:
            output_dataset = Dataset.from_dict(dataset_dict)
            
        def format_cell_features(example):
            # Store original uncropped input_ids in separate feature
            if keep_uncropped_input_ids:
                example['input_ids_uncropped'] = example['input_ids']
                example['length_uncropped'] = len(example['input_ids'])

            # Truncate/Crop input_ids to size 2,048
            example['input_ids'] = example['input_ids'][0:2048]
            example['length'] = len(example['input_ids'])

            return example

        output_dataset_truncated = output_dataset.map(
            format_cell_features,
            num_proc=self.nproc
        )
        return output_dataset_truncated

print("Tokenizer functions successfully defined.")

###############################
# run tokenise.py
###############################
print("Running tokenise.py...")
import os
import scanpy
import anndata



# Inputs / Ouputs
token_outprefix=sys.argv[1]
loom_outpath=os.getcwd()  + "/"+ token_outprefix # This to get the cromwell-root/
print("Outside tokeniser function ::: Path to where we save the data: \n" \
            + loom_outpath + "\n" \
            + token_outprefix + "\n")

loom_inpath=sys.argv[2]
print("loom_inpath: \n" + loom_inpath)



# Get Tokeniser instance, and call data tokeniser method
print("Running tk.tokenize_data on: \n" \
       + loom_inpath + "\n" \
       + loom_outpath + "\n" \
       + token_outprefix + "\n")
tk = TranscriptomeTokenizer(nproc=1)
tk.tokenize_data(loom_inpath, loom_outpath, token_outprefix)



# Checks
print("Python -- pwd:")
print(os.getcwd())

print("Python -- contents of current dir:")
print(os.listdir(loom_outpath))

print("Python -- contents of dataset dir:")
print(os.listdir(loom_outpath+"/"+token_outprefix))

print('DATA HAS BEEN TOKENIZED...\n')
print('SUCCESS')

# -------------------------------------------------------------------

