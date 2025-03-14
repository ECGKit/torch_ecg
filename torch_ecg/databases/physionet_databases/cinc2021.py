# -*- coding: utf-8 -*-

import io
import json
import os
import posixpath
import re
import time
import warnings
from copy import deepcopy
from datetime import datetime
from numbers import Real
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd
import scipy.signal as SS
import wfdb
from scipy.io import loadmat
from tqdm.auto import tqdm

from ...cfg import CFG, DEFAULTS
from ...utils import ecg_arrhythmia_knowledge as EAK
from ...utils.download import http_get
from ...utils.misc import add_docstring, get_record_list_recursive3, list_sum, ms2samples
from ...utils.utils_data import ensure_siglen
from ..aux_data.cinc2021_aux_data import (
    df_weights_abbr,
    dx_cooccurrence_all_fp,
    dx_mapping_all,
    dx_mapping_scored,
    equiv_class_dict,
    load_weights,
)
from ..base import DEFAULT_FIG_SIZE_PER_SEC, DataBaseInfo, PhysioNetDataBase, _PlotCfg

__all__ = [
    "CINC2021",
    "compute_metrics",
    "compute_metrics_detailed",
]


_CINC2021_INFO = DataBaseInfo(
    title="""
    Will Two Do? Varying Dimensions in Electrocardiography:
    The PhysioNet/Computing in Cardiology Challenge 2021
    """,
    about="""
    0. goal: build an algorithm that can classify cardiac abnormalities from either

        - twelve-lead (I, II, III, aVR, aVL, aVF, V1, V2, V3, V4, V5, V6)
        - six-lead (I, II, III, aVL, aVR, aVF),
        - four-lead (I, II, III, V2)
        - three-lead (I, II, V2)
        - two-lead (I, II)

    1. tranches of data:

        - CPSC2018 (tranches A and B of CinC2020, ref. [4]_):
            contains 13,256 ECGs (6,877 from tranche A, 3,453 from tranche B),
            10,330 ECGs shared as training data, 1,463 retained as validation data,
            and 1,463 retained as test data.
            Each recording is between 6 and 144 seconds long with a sampling frequency of 500 Hz
        - INCARTDB (tranche C of CinC2020, ref. [4]_):
            contains 75 annotated ECGs,
            all shared as training data, extracted from 32 Holter monitor recordings.
            Each recording is 30 minutes long with a sampling frequency of 257 Hz
        - PTB (PTB and PTB-XL, tranches D and E of CinC2020, ref. [5]_ and [6]_):
            contains 22,353 ECGs,
            516 + 21,837, all shared as training data.
            Each recording is between 10 and 120 seconds long,
            with a sampling frequency of either 500 (PTB-XL) or 1,000 (PTB) Hz
        - Georgia (tranche F of CinC2020, ref. [3]_):
            contains 20,678 ECGs,
            10,334 ECGs shared as training data, 5,167 retained as validation data,
            and 5,167 retained as test data.
            Each recording is between 5 and 10 seconds long with a sampling frequency of 500 Hz
        - American (NEW, UNDISCLOSED):
            contains 10,000 ECGs,
            all retained as test data,
            geographically distinct from the Georgia database.
            Perhaps is the main part of the hidden test set of CinC2020
        - CUSPHNFH (NEW, the Chapman University, Shaoxing People’s Hospital and Ningbo First Hospital database)
            contains 45,152 ECGS,
            all shared as training data.
            Each recording is 10 seconds long with a sampling frequency of 500 Hz
            this tranche contains two subsets:

                - Chapman_Shaoxing: "JS00001" - "JS10646"
                - Ningbo: "JS10647" - "JS45551"

       All files can be downloaded from [8]_ or [9]_.

    2. only a part of diagnosis_abbr (diseases that appear in the labels of the 6 tranches of training data) are used in the scoring function, while others are ignored. The scored diagnoses were chosen based on prevalence of the diagnoses in the training data, the severity of the diagnoses, and the ability to determine the diagnoses from ECG recordings. The ignored diagnosis_abbr can be put in a a "non-class" group.
    3. the (updated) scoring function has a scoring matrix with nonzero off-diagonal elements. This scoring function reflects the clinical reality that some misdiagnoses are more harmful than others and should be scored accordingly. Moreover, it reflects the fact that confusing some classes is much less harmful than confusing other classes.
    4. all data are recorded in the leads ordering of

       .. code-block:: python

            ["I", "II", "III", "aVR", "aVL", "aVF", "V1", "V2", "V3", "V4", "V5", "V6"]

       using for example the following code:

       .. code-block:: python

            >>> db_dir = "/media/cfs/wenhao71/data/CinC2021/"
            >>> working_dir = "./working_dir"
            >>> dr = CINC2021(db_dir=db_dir,working_dir=working_dir)
            >>> set_leads = []
            >>> for tranche, l_rec in dr.all_records.items():
            ...     for rec in l_rec:
            ...         ann = dr.load_ann(rec)
            ...         leads = ann["df_leads"]["lead_name"].values.tolist()
            ...     if leads not in set_leads:
            ...         set_leads.append(leads)
    5. Challenge official website [1]_. Webpage of the database on PhysioNet [2]_.

    """,
    note="""
    1. The datasets have been roughly processed to have a uniform format, hence differ from their original resource (e.g. differe in sampling frequency, sample duration, etc.)
    2. The original datasets might have richer metadata (especially those from PhysioNet), which can be fetched from corresponding reader's docstring or website of the original source
    3. Each sub-dataset might have its own organizing scheme of data, which should be carefully dealt with
    4. There are few "absolute" diagnoses in 12 lead ECGs, where large discrepancies in the interpretation of the ECG can be found even inspected by experts. There is inevitably something lost in translation, especially when you do not have the context. This doesn"t mean making an algorithm isn't important
    5. The labels are noisy, which one has to deal with in all real world data
    6. each line of the following classes are considered the same (in the scoring matrix):

        - RBBB, CRBBB (NOT including IRBBB)
        - PAC, SVPB
        - PVC, VPB

    7. unfortunately, the newly added tranches (C - F) have baseline drift and are much noisier. In contrast, CPSC data have had baseline removed and have higher SNR
    8. on Aug. 1, 2020, adc gain (including "resolution", "ADC"? in .hea files) of datasets INCART, PTB, and PTB-xl (tranches C, D, E) are corrected. After correction, (the .tar files of) the 3 datasets are all put in a "WFDB" subfolder. In order to keep the structures consistant, they are moved into "Training_StPetersburg", "Training_PTB", "WFDB" as previously. Using the following code, one can check the adc_gain and baselines of each tranche:

       .. code-block:: python

            >>> db_dir = "/media/cfs/wenhao71/data/CinC2021/"
            >>> working_dir = "./working_dir"
            >>> dr = CINC2021(db_dir=db_dir,working_dir=working_dir)
            >>> resolution = {tranche: set() for tranche in "ABCDEF"}
            >>> baseline = {tranche: set() for tranche in "ABCDEF"}
            >>> for tranche, l_rec in dr.all_records.items():
            ...     for rec in l_rec:
            ...         ann = dr.load_ann(rec)
            ...         resolution[tranche] = resolution[tranche].union(set(ann["df_leads"]["adc_gain"]))
            ...         baseline[tranche] = baseline[tranche].union(set(ann["df_leads"]["baseline"]))
            >>> print(resolution, baseline)
            {"A": {1000.0}, "B": {1000.0}, "C": {1000.0}, "D": {1000.0}, "E": {1000.0}, "F": {1000.0}} {"A": {0}, "B": {0}, "C": {0}, "D": {0}, "E": {0}, "F": {0}}

    9. the .mat files all contain digital signals, which has to be converted to physical values using adc gain, basesline, etc. in corresponding .hea files. :func:`wfdb.rdrecord` has already done this conversion, hence greatly simplifies the data loading process. NOTE that there"s a difference when using :func:`wfdb.rdrecord`: data from `loadmat` are in "channel_first" format, while `wfdb.rdrecord.p_signal` produces data in the "channel_last" format
    10. there are 3 equivalent (2 classes are equivalent if the corr. value in the scoring matrix is 1): (RBBB, CRBBB), (PAC, SVPB), (PVC, VPB)
    11. in the newly (Feb., 2021) created dataset (ref. [7]_), header files of each subset were gathered into one separate compressed file. This is due to the fact that updates on the dataset are almost always done in the header files. The correct usage of ref. [7], after uncompressing, is replacing the header files in the folder `All_training_WFDB` by header files from the 6 folders containing all header files from the 6 subsets. This procedure has to be done, since `All_training_WFDB` contains the very original headers with baselines: {"A": {1000.0}, "B": {1000.0}, "C": {1000.0}, "D": {2000000.0}, "E": {200.0}, "F": {4880.0}} (the last 3 are NOT correct)
    12. IMPORTANT: organization of the total dataset:
        either one moves all training records into ONE folder,
        or at least one moves the subsets Chapman_Shaoxing (WFDB_ChapmanShaoxing) and Ningbo (WFDB_Ningbo) into ONE folder, or use the data WFDB_ShaoxingUniv which is the union of WFDB_ChapmanShaoxing and WFDB_Ningbo
    """,
    usage=[
        "ECG arrhythmia detection",
    ],
    issues="""
    1. reading the .hea files, baselines of all records are 0, however it is not the case if one plot the signal
    2. about half of the LAD records satisfy the "2-lead" criteria, but fail for the "3-lead" criteria, which means that their axis is (-30°, 0°) which is not truely LAD
    3. (Aug. 15, 2020; resolved, and changed to 1000) tranche F, the Georgia subset, has ADC gain 4880 which might be too high. Thus obtained voltages are too low. 1000 might be a suitable (correct) value of ADC gain for this tranche just as the other tranches.
    4. "E04603" (all leads), "E06072" (chest leads, epecially V1-V3), "E06909" (lead V2), "E07675" (lead V3), "E07941" (lead V6), "E08321" (lead V6) has exceptionally large values at rpeaks, reading (`load_data`) these two records using `wfdb` would bring in `nan` values. One can check using the following code

       .. code-block:: python

            >>> rec = "E04603"
            >>> dr.plot(rec, dr.load_data(rec, backend="scipy", units="uv"))  # currently raising error

    5. many records (headers) have duplicate labels. For example, many records in the Georgia subset has duplicate "PAC" ("284470004") label
    6. some records in tranche G has #Dx ending with "," (at least "JS00344"), or consecutive "," (at least "JS03287") in corresponding .hea file
    7. tranche G has 2 Dx ("251238007", "6180003") which are listed in neither of dx_mapping_scored.csv nor dx_mapping_unscored.csv
    8. about 68 records from tranche G has `nan` values loaded via :func:`wfdb.rdrecord`, which might be caused by motion artefact in some leads
    9. "Q0400", "Q2961" are completely flat (constant), while many other records have flat leads, especially V1-V6 leads
    """,
    references=[
        "https://moody-challenge.physionet.org/2021/",
        "https://physionet.org/content/challenge-2021/",
        "https://moody-challenge.physionet.org/2020/",
        "http://2018.icbeb.org/",
        "https://physionet.org/content/incartdb/",
        "https://physionet.org/content/ptbdb/",
        "https://physionet.org/content/ptb-xl/",
        "(deprecated) https://storage.cloud.google.com/physionet-challenge-2020-12-lead-ECG-public/",
        "(recommended) https://storage.cloud.google.com/physionetchallenge2021-public-datasets/",
    ],
    doi=[
        "10.23919/cinc53138.2021.9662687",
        "10.13026/JZ9P-0M02",
    ],
)


@add_docstring(_CINC2021_INFO.format_database_docstring(), mode="prepend")
class CINC2021(PhysioNetDataBase):
    """
    Parameters
    ----------
    db_dir : `path-like`, optional
        Storage path of the database.
        If not specified, data will be fetched from Physionet.
    working_dir : `path-like`, optional
        Working directory, to store intermediate files and log files.
    verbose : int, default 1
        Level of logging verbosity.
    kwargs : dict, optional
        Auxilliary key word arguments.

    """

    def __init__(
        self,
        db_dir: Optional[Union[str, bytes, os.PathLike]] = None,
        working_dir: Optional[Union[str, bytes, os.PathLike]] = None,
        verbose: int = 1,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            db_name="challenge-2021",
            db_dir=db_dir,
            working_dir=working_dir,
            verbose=verbose,
            **kwargs,
        )

        self.rec_ext = "mat"
        self.ann_ext = "hea"

        self.db_tranches = list("ABCDEFG")
        self.tranche_names = CFG(
            {
                "A": "CPSC",
                "B": "CPSC_Extra",
                "C": "StPetersburg",
                "D": "PTB",
                "E": "PTB_XL",
                "F": "Georgia",
                "G": "CUSPHNFH",
            }
        )
        self.rec_prefix = CFG(
            {
                "A": "A",
                "B": "Q",
                "C": "I",
                "D": "S",
                "E": "HR",
                "F": "E",
                "G": "JS",
            }
        )

        self.fs = CFG(
            {
                "A": 500,
                "B": 500,
                "C": 257,
                "D": 1000,
                "E": 500,
                "F": 500,
                "G": 500,
            }
        )
        self.spacing = CFG({t: 1000 / f for t, f in self.fs.items()})

        self.all_leads = deepcopy(EAK.Standard12Leads)

        self.df_ecg_arrhythmia = dx_mapping_all[["Dx", "SNOMEDCTCode", "Abbreviation"]]
        self.ann_items = [
            "rec_name",
            "nb_leads",
            "fs",
            "nb_samples",
            "datetime",
            "age",
            "sex",
            "diagnosis",
            "df_leads",
            "medical_prescription",
            "history",
            "symptom_or_surgery",
        ]
        self.label_trans_dict = equiv_class_dict.copy()

        # self.value_correction_factor = CFG({tranche:1 for tranche in self.db_tranches})
        # self.value_correction_factor.F = 4.88  # ref. ISSUES 3

        # fmt: off
        self.exceptional_records = [
            "I0002", "I0069", "E04603", "E06072",
            "E06909", "E07675", "E07941", "E08321",
        ]  # ref. ISSUES 4
        self.exceptional_records += [  # ref. ISSUE 8
            "JS10765", "JS10767", "JS10890", "JS10951", "JS11887", "JS11897",
            "JS11956", "JS12751", "JS13181", "JS14161", "JS14343", "JS14627",
            "JS14659", "JS15624", "JS16169", "JS16222", "JS16813", "JS19309",
            "JS19708", "JS20330", "JS20656", "JS21144", "JS21617", "JS21668",
            "JS21701", "JS21853", "JS21881", "JS23116", "JS23450", "JS23482",
            "JS23588", "JS23786", "JS23950", "JS24016", "JS25106", "JS25322",
            "JS25458", "JS26009", "JS26130", "JS26145", "JS26245", "JS26605",
            "JS26793", "JS26843", "JS26977", "JS27034", "JS27170", "JS27271",
            "JS27278", "JS27407", "JS27460", "JS27835", "JS27985", "JS28075",
            "JS28648", "JS28757", "JS33280", "JS34479", "JS34509", "JS34788",
            "JS34868", "JS34879", "JS35050", "JS35065", "JS35192", "JS35654",
            "JS35727", "JS36015", "JS36018", "JS36189", "JS36244", "JS36568",
            "JS36731", "JS37105", "JS37173", "JS37176", "JS37439", "JS37592",
            "JS37609", "JS37781", "JS38231", "JS38252", "JS41844", "JS41908",
            "JS41935", "JS42026", "JS42330",
        ]
        self.exceptional_records += [
            "Q0400", "Q2961",
        ]  # ref. ISSUE 9
        # TODO: exceptional records can be resolved via reading using `scipy` backend,
        # with noise removal using `remove_spikes_naive` from `signal_processing` module
        # currently for simplicity, exceptional records would be ignored
        # fmt: on

        self.db_dir_base = Path(db_dir)
        self._all_records = None
        self.__all_records = None
        self._stats = pd.DataFrame()
        self._stats_columns = {
            "record",
            "tranche",
            "tranche_name",
            "nb_leads",
            "fs",
            "nb_samples",
            "age",
            "sex",
            "medical_prescription",
            "history",
            "symptom_or_surgery",
            "diagnosis",
            "diagnosis_scored",  # in the form of abbreviations
        }
        self._ls_rec()  # loads file system structures into `self._all_records`
        self._aggregate_stats(fast=True)

        self._diagnoses_records_list = None
        # self._ls_diagnoses_records()

    def get_subject_id(self, rec: Union[str, int]) -> int:
        """Attach a unique subject ID for the record.

        Parameters
        ----------
        rec : str or int
            Record name or index of the record in :attr:`all_records`.

        Returns
        -------
        int
            Subject ID associated with the record.

        """
        if isinstance(rec, int):
            rec = self[rec]
        s2d = {
            "A": "11",
            "B": "12",
            "C": "21",
            "D": "31",
            "E": "32",
            "F": "41",
            "G": "51",
        }
        s2d = {self.rec_prefix[k]: v for k, v in s2d.items()}
        prefix = "".join(re.findall(r"[A-Z]", rec))
        n = rec.replace(prefix, "")
        sid = int(f"{s2d[prefix]}{'0'*(8-len(n))}{n}")
        return sid

    def _ls_rec(self) -> None:
        """Find all records in the database directory
        and store them (path, metadata, etc.) in some private attributes.
        """
        filename = f"{self.db_name}-record_list.json"
        record_list_fp = self.db_dir_base / filename
        write_file = False
        self._df_records = pd.DataFrame()
        self._all_records = CFG({tranche: [] for tranche in self.db_tranches})
        if record_list_fp.is_file():
            for k, v in json.loads(record_list_fp.read_text()).items():
                if k in self.tranche_names:
                    self._all_records[k] = v
            for tranche in self.db_tranches:
                df_tmp = pd.DataFrame(self._all_records[tranche], columns=["path"])
                df_tmp["tranche"] = tranche
                self._df_records = pd.concat([self._df_records, df_tmp], ignore_index=True)
            self._df_records["path"] = self._df_records.path.apply(lambda x: Path(x))
            self._df_records["record"] = self._df_records.path.apply(lambda x: x.stem)

            self._df_records = self._df_records[
                self._df_records.path.apply(lambda x: x.with_suffix(f".{self.rec_ext}").is_file())
            ]

        if len(self._df_records) == 0 or any(len(v) == 0 for v in self._all_records.values()):
            original_len = len(self._df_records)
            self._df_records = pd.DataFrame()
            self.logger.info("Please wait patiently to let the reader find all records of all the tranches...")
            start = time.time()
            rec_patterns_with_ext = {
                tranche: f"^{self.rec_prefix[tranche]}(?:\\d+)\\.{self.rec_ext}$" for tranche in self.db_tranches
            }
            self._all_records = get_record_list_recursive3(str(self.db_dir_base), rec_patterns_with_ext, relative=False)
            to_save = deepcopy(self._all_records)
            for tranche in self.db_tranches:
                df_tmp = pd.DataFrame(self._all_records[tranche], columns=["path"])
                df_tmp["tranche"] = tranche
                self._df_records = pd.concat([self._df_records, df_tmp], ignore_index=True)
            self._df_records["path"] = self._df_records.path.apply(lambda x: Path(x))
            self._df_records["record"] = self._df_records.path.apply(lambda x: x.stem)

            self.logger.info(f"Done in {time.time() - start:.5f} seconds!")

            if len(self._df_records) > original_len:
                write_file = True

            if write_file:
                record_list_fp.write_text(json.dumps(to_save))

        if len(self._df_records) > 0 and self._subsample is not None:
            df_tmp = pd.DataFrame()
            for tranche in self.db_tranches:
                size = int(round(len(self._all_records[tranche]) * self._subsample))
                if size > 0:
                    df_tmp = pd.concat(
                        [
                            df_tmp,
                            self._df_records[self._df_records.tranche == tranche].sample(
                                size, random_state=DEFAULTS.SEED, replace=False
                            ),
                        ],
                        ignore_index=True,
                    )
            if len(df_tmp) == 0:
                size = min(
                    len(self._df_records),
                    max(1, int(round(self._subsample * len(self._df_records)))),
                )
                df_tmp = self._df_records.sample(size, random_state=DEFAULTS.SEED, replace=False)
            del self._df_records
            self._df_records = df_tmp.copy()
            del df_tmp
            self._all_records = CFG(
                {
                    tranche: sorted(
                        [Path(x).stem for x in self._df_records[self._df_records.tranche == tranche]["path"].values]
                    )
                    for tranche in self.db_tranches
                }
            )

        self._all_records = CFG(
            {tranche: sorted([Path(x).stem for x in self._all_records[tranche]]) for tranche in self.db_tranches}
        )
        self.__all_records = list_sum(self._all_records.values())

        self._df_records.set_index("record", inplace=True)
        self._df_records["fs"] = self._df_records.tranche.apply(lambda x: self.fs[x])

        # TODO: perhaps we can load labels and metadata of all records into `self._df_records` here

    def _aggregate_stats(self, fast: bool = False, force_reload: bool = False) -> None:
        """Aggregate stats on the whole dataset.

        Parameters
        ----------
        fast : bool, default False
            If True, only the cached stats will be loaded,
            otherwise the stats will be aggregated from scratch.
            Ignored if `force_reload` is True.
        force_reload : bool, default False
            If True, the stats will be aggregated from scratch.

        Returns
        -------
        None

        """
        stats_file = f"{self.db_name}-stats.csv"
        list_sep = ";"
        stats_file_fp = self.db_dir_base / stats_file
        if stats_file_fp.is_file():
            self._stats = pd.read_csv(stats_file_fp, keep_default_na=False)
        if force_reload or (not fast and (self._stats.empty or self._stats_columns != set(self._stats.columns))):
            self.logger.info("Please wait patiently to let the reader collect statistics on the whole dataset...")
            start = time.time()
            self._stats = self._df_records.copy(deep=True)
            self._stats["record"] = self._stats.index
            self._stats = self._stats.reset_index(drop=True)
            self._stats.drop(columns="path", inplace=True)
            self._stats["tranche_name"] = self._stats["tranche"].apply(lambda t: self.tranche_names[t])
            for k in [
                "diagnosis",
                "diagnosis_scored",
            ]:
                self._stats[k] = ""  # otherwise cells in the first row would be str instead of list
            with tqdm(
                self._stats.iterrows(),
                total=len(self._stats),
                desc="Aggregating stats",
                unit="record",
                dynamic_ncols=True,
                mininterval=1.0,
                disable=(self.verbose < 1),
            ) as pbar:
                for idx, row in pbar:
                    ann_dict = self.load_ann(row["record"])
                    for k in [
                        "nb_leads",
                        "fs",
                        "nb_samples",
                        "age",
                        "sex",
                        "medical_prescription",
                        "history",
                        "symptom_or_surgery",
                    ]:
                        self._stats.at[idx, k] = ann_dict[k]
                    for k in [
                        "diagnosis",
                        "diagnosis_scored",
                    ]:
                        self._stats.at[idx, k] = ann_dict[k]["diagnosis_abbr"]
            for k in ["nb_leads", "fs", "nb_samples"]:
                self._stats[k] = self._stats[k].astype(int)
            _stats_to_save = self._stats.copy()
            for k in [
                "diagnosis",
                "diagnosis_scored",
            ]:
                _stats_to_save[k] = _stats_to_save[k].apply(lambda lst: list_sep.join(lst))
            _stats_to_save.to_csv(stats_file_fp, index=False)
            self.logger.info(f"Done in {time.time() - start:.5f} seconds!")
        else:
            self.logger.info("converting dtypes of columns `diagnosis` and `diagnosis_scored`...")
            for k in [
                "diagnosis",
                "diagnosis_scored",
            ]:
                for idx, row in self._stats.iterrows():
                    self._stats.at[idx, k] = list(filter(lambda v: len(v) > 0, row[k].split(list_sep)))

    @property
    def all_records(self) -> Dict[str, List[str]]:
        """List of all records in the database."""
        if self._all_records is None:
            self._ls_rec()
        return self._all_records

    @property
    def df_stats(self) -> pd.DataFrame:
        """Dataframe of stats on the whole dataset."""
        if self._stats.empty:
            warnings.warn(
                "the dataframe of stats is empty, try using _aggregate_stats",
                RuntimeWarning,
            )
        return self._stats

    def _ls_diagnoses_records(self) -> None:
        """List all the records for all diagnoses."""
        filename = f"{self.db_name}-diagnoses_records_list.json"
        dr_fp = self.db_dir_base / filename
        if dr_fp.is_file():
            self._diagnoses_records_list = json.loads(dr_fp.read_text())
        else:
            self.logger.info("Please wait several minutes patiently to let the reader list records for each diagnosis...")
            self._diagnoses_records_list = {d: [] for d in df_weights_abbr.columns.values.tolist()}
            if self._stats.empty:
                self._aggregate_stats()
            for d in df_weights_abbr.columns.values.tolist():
                self._diagnoses_records_list[d] = sorted(
                    self._stats[self._stats["diagnosis_scored"].apply(lambda lst: d in lst)]["record"].tolist()
                )
            dr_fp.write_text(json.dumps(self._diagnoses_records_list))
        self._diagnoses_records_list = CFG(self._diagnoses_records_list)

    @property
    def diagnoses_records_list(self) -> Dict[str, List[str]]:
        """List of all records for each diagnosis."""
        if self._diagnoses_records_list is None:
            self._ls_diagnoses_records()
        return self._diagnoses_records_list

    def _get_tranche(self, rec: Union[str, int]) -> str:
        """Get the symbol of the tranche of the record.

        Parameters
        ----------
        rec : str or int
            Record name or index of the record in :attr:`all_records`.

        Returns
        -------
        tranche : str
            Symbol of the tranche, ref. `self.rec_prefix`.

        """
        if isinstance(rec, int):
            rec = self[rec]
        tranche = self._df_records.loc[rec, "tranche"]
        return tranche

    def get_absolute_path(self, rec: Union[str, int], extension: Optional[str] = None) -> Path:
        """Get the absolute path of the record.

        Parameters
        ----------
        rec : str or int
            Record name or index of the record in :attr:`all_records`.
        extension : str, optional
            Extension of the file.

        Returns
        -------
        abs_fp : pathlib.Path
            Absolute path of the file.

        """
        if isinstance(rec, int):
            rec = self[rec]
        abs_fp = self._df_records.loc[rec, "path"]
        if extension is not None:
            if not extension.startswith("."):
                extension = f".{extension}"
            abs_fp = abs_fp.with_suffix(extension)
        return abs_fp

    def get_data_filepath(self, rec: Union[str, int], with_ext: bool = True) -> Path:
        """Get the absolute file path of the data file of the record.

        Parameters
        ----------
        rec : str or int
            Record name or index of the record in :attr:`all_records`.
        with_ext : bool, default True
            If True, the returned file path comes with file extension,
            otherwise without file extension,
            which is useful for `wfdb` functions.

        Returns
        -------
        pathlib.Path
            Absolute file path of the data file of the record.

        """
        return self.get_absolute_path(rec, self.rec_ext if with_ext else None)

    @add_docstring(get_data_filepath.__doc__.replace("data file", "header file"))
    def get_header_filepath(self, rec: Union[str, int], with_ext: bool = True) -> Path:
        return self.get_absolute_path(rec, self.ann_ext if with_ext else None)

    @add_docstring(get_header_filepath.__doc__)
    def get_ann_filepath(self, rec: Union[str, int], with_ext: bool = True) -> Path:
        """alias for `get_header_filepath`"""
        fp = self.get_header_filepath(rec, with_ext=with_ext)
        return fp

    def load_data(
        self,
        rec: Union[str, int],
        leads: Optional[Union[str, int, Sequence[Union[str, int]]]] = None,
        data_format: str = "channel_first",
        backend: Literal["wfdb", "scipy"] = "wfdb",
        units: Literal["mV", "μV", "uV", "muV", None] = "mV",
        fs: Optional[Real] = None,
        return_fs: bool = False,
    ) -> Union[np.ndarray, Tuple[np.ndarray, Real]]:
        """Load physical (converted from digital) ECG data.

        Parameters
        ----------
        rec : str or int
            Record name or index of the record in :attr:`all_records`.
        leads : str or int or Sequence[str] or Sequence[int], optional
            The leads to load, can be a single lead or a list of leads.
        data_format : str, default "channel_first"
            Format of the ECG data,
            "channel_last" (alias "lead_last"), or
            "channel_first" (alias "lead_first")
        backend : {"wfdb", "scipy"}, default "wfdb"
            The backend data reader.
        units : str or None, default "mV"
            Units of the output signal, can also be "μV" (aliases "uV", "muV").
            None for digital data, without digital-to-physical conversion.
        fs : numbers.Real, optional
            Sampling frequency of the output signal.
            If not None, the loaded data will be resampled to this frequency,
            otherwise, the original sampling frequency will be used.
        return_fs : bool, default False
            Whether to return the sampling frequency of the output signal.

        Returns
        -------
        data : numpy.ndarray
            The loaded ECG data.
        data_fs : numbers.Real, optional
            Sampling frequency of the output signal.
            Returned if `return_fs` is True.

        """
        if isinstance(rec, int):
            rec = self[rec]
        assert data_format.lower() in [
            "channel_first",
            "lead_first",
            "channel_last",
            "lead_last",
        ], f"Invalid data_format: `{data_format}`"
        # tranche = self._get_tranche(rec)
        if leads is None or (isinstance(leads, str) and leads.lower() == "all"):
            _leads = self.all_leads
        elif isinstance(leads, str):
            _leads = [leads]
        elif isinstance(leads, int):
            _leads = [self.all_leads[leads]]
        else:
            _leads = [ld if isinstance(ld, str) else self.all_leads[ld] for ld in leads]
        # if tranche in "CD" and fs == 500:  # resample will be done at the end of the function
        #     data = self.load_resampled_data(rec)
        if backend.lower() == "wfdb":
            rec_fp = self.get_data_filepath(rec, with_ext=False)
            # p_signal or d_signal of "lead_last" format
            wfdb_rec = wfdb.rdrecord(
                str(rec_fp),
                physical=units is not None,
                channel_names=_leads,
                return_res=DEFAULTS.DTYPE.INT,
            )
            if units is None:
                data = wfdb_rec.d_signal.T
            else:
                data = wfdb_rec.p_signal.T
            # lead_units = np.vectorize(lambda s: s.lower())(wfdb_rec.units)
        elif backend.lower() == "scipy":
            # loadmat of "lead_first" format
            rec_fp = self.get_data_filepath(rec, with_ext=True)
            data = loadmat(str(rec_fp))["val"]
            if units is not None:
                header_info = self.load_ann(rec, raw=False)["df_leads"]
                baselines = header_info["baseline"].values.reshape(data.shape[0], -1)
                adc_gain = header_info["adc_gain"].values.reshape(data.shape[0], -1)
                data = np.asarray(data - baselines, dtype=DEFAULTS.DTYPE.NP) / adc_gain
            leads_ind = [self.all_leads.index(item) for item in _leads]
            data = data[leads_ind, :]
            # lead_units = np.vectorize(lambda s: s.lower())(header_info["df_leads"]["adc_units"].values)
        else:
            raise ValueError(f"backend `{backend.lower()}` not supported for loading data")

        # ref. ISSUES 3, for multiplying `value_correction_factor`
        # data = data * self.value_correction_factor[tranche]

        if units is not None and units.lower() in ["uv", "μv", "muv"]:
            data = data * 1000

        rec_fs = self.get_fs(rec, from_hea=True)
        if fs is not None and fs != rec_fs:
            data = SS.resample_poly(data, fs, rec_fs, axis=1).astype(data.dtype)
            data_fs = fs
        else:
            data_fs = rec_fs
        # if fs is not None and fs != self.fs[tranche]:
        #     data = SS.resample_poly(data, fs, self.fs[tranche], axis=1)

        if data_format.lower() in ["channel_last", "lead_last"]:
            data = data.T

        if return_fs:
            return data, data_fs
        return data

    def load_ann(self, rec: Union[str, int], raw: bool = False) -> Union[dict, str]:
        """Load the annotations of the record.

        The annotations (header) are stored in the .hea files.

        Parameters
        ----------
        rec : str or int
            Record name or index of the record in :attr:`all_records`.
        raw : bool, default False
            If True, the raw annotations without
            being parsed will be returned.

        Returns
        -------
        ann_dict : dict or str
            The annotations with items listed in `self.ann_items`.

        """
        if isinstance(rec, int):
            rec = self[rec]
        # tranche = self._get_tranche(rec)
        ann_fp = self.get_ann_filepath(rec, with_ext=True)
        header_data = ann_fp.read_text().splitlines()

        if raw:
            ann_dict = "\n".join(header_data)
            return ann_dict

        ann_dict = self._load_ann_wfdb(rec, header_data)
        return ann_dict

    def _load_ann_wfdb(self, rec: Union[str, int], header_data: List[str]) -> dict:
        """Load annotations (header) using :func:`wfdb.rdheader`.

        Parameters
        ----------
        rec : str or int
            Record name or index of the record in :attr:`all_records`.
        header_data : List[str]
            List of lines read directly from a header file.
            This data will be used, since `datetime` is
            not well parsed by :func:`wfdb.rdheader`.

        Returns
        -------
        ann_dict : dict
            The annotations with items listed in `self.ann_items`.

        """
        if isinstance(rec, int):
            rec = self[rec]
        header_fp = self.get_header_filepath(rec, with_ext=False)
        header_reader = wfdb.rdheader(str(header_fp))
        ann_dict = {}
        (
            ann_dict["rec_name"],
            ann_dict["nb_leads"],
            ann_dict["fs"],
            ann_dict["nb_samples"],
        ) = header_data[0].split(
            " "
        )[0:4]
        if len(header_data[0].split(" ")) >= 6:
            ann_dict["datetime"], daytime = header_data[0].split(" ")[4:6]
        else:
            ann_dict["datetime"], daytime = None, None

        ann_dict["nb_leads"] = int(ann_dict["nb_leads"])
        ann_dict["fs"] = int(ann_dict["fs"])
        ann_dict["nb_samples"] = int(ann_dict["nb_samples"])
        if ann_dict["datetime"] is not None and daytime is not None:
            try:
                ann_dict["datetime"] = datetime.strptime(" ".join([ann_dict["datetime"], daytime]), "%d-%b-%Y %H:%M:%S")
            except Exception:
                pass
        try:  # see NOTE. 1.
            ann_dict["age"] = int([line for line in header_reader.comments if "Age" in line][0].split(":")[-1].strip())
        except Exception:
            ann_dict["age"] = np.nan
        try:  # only "10726" has "NaN" sex
            ann_dict["sex"] = (
                [line for line in header_reader.comments if "Sex" in line][0].split(":")[-1].strip().replace("NaN", "Unknown")
            )
        except Exception:
            ann_dict["sex"] = "Unknown"
        try:
            ann_dict["medical_prescription"] = (
                [line for line in header_reader.comments if "Rx" in line][0].split(":")[-1].strip()
            )
        except Exception:
            ann_dict["medical_prescription"] = "Unknown"
        try:
            ann_dict["history"] = [line for line in header_reader.comments if "Hx" in line][0].split(":")[-1].strip()
        except Exception:
            ann_dict["history"] = "Unknown"
        try:
            ann_dict["symptom_or_surgery"] = [line for line in header_reader.comments if "Sx" in line][0].split(":")[-1].strip()
        except Exception:
            ann_dict["symptom_or_surgery"] = "Unknown"

        # l_Dx = [line for line in header_reader.comments if "Dx" in line][0].split(": ")[-1].split(",")
        # ref. ISSUE 6
        l_Dx = [line for line in header_reader.comments if "Dx" in line][0].split(":")[-1].strip().split(",")
        l_Dx = [d for d in l_Dx if len(d) > 0]
        ann_dict["diagnosis"], ann_dict["diagnosis_scored"] = self._parse_diagnosis(l_Dx)

        df_leads = pd.DataFrame()
        cols = [
            "file_name",
            "fmt",
            "byte_offset",
            "adc_gain",
            "units",
            "adc_res",
            "adc_zero",
            "baseline",
            "init_value",
            "checksum",
            "block_size",
            "sig_name",
        ]
        for k in cols:
            df_leads[k] = header_reader.__dict__[k]
        df_leads = df_leads.rename(
            columns={
                "sig_name": "lead_name",
                "units": "adc_units",
                "file_name": "filename",
            }
        )
        df_leads.index = df_leads["lead_name"]
        df_leads.index.name = None
        ann_dict["df_leads"] = df_leads

        return ann_dict

    def _parse_diagnosis(self, l_Dx: List[str]) -> Tuple[dict, dict]:
        """Parse diagnosis from a list of strings.

        Parameters
        ----------
        l_Dx : List[str]
            Raw information of diagnosis, read from a header file.

        Returns
        -------
        diag_dict : dict
            Diagnosis, including SNOMED CT Codes,
            fullnames and abbreviations of each diagnosis.
        diag_scored_dict : dict
            The scored items in `diag_dict`.

        """
        diag_dict, diag_scored_dict = {}, {}
        # try:
        diag_dict["diagnosis_code"] = [item for item in l_Dx if item in dx_mapping_all["SNOMEDCTCode"].tolist()]
        # in case not listed in dx_mapping_all
        left = [item for item in l_Dx if item not in dx_mapping_all["SNOMEDCTCode"].tolist()]
        # selection = dx_mapping_all["SNOMEDCTCode"].isin(diag_dict["diagnosis_code"])
        # diag_dict["diagnosis_abbr"] = dx_mapping_all[selection]["Abbreviation"].tolist()
        # diag_dict["diagnosis_fullname"] = dx_mapping_all[selection]["Dx"].tolist()
        diag_dict["diagnosis_abbr"] = [
            dx_mapping_all[dx_mapping_all["SNOMEDCTCode"] == dc]["Abbreviation"].values[0] for dc in diag_dict["diagnosis_code"]
        ] + left
        diag_dict["diagnosis_fullname"] = [
            dx_mapping_all[dx_mapping_all["SNOMEDCTCode"] == dc]["Dx"].values[0] for dc in diag_dict["diagnosis_code"]
        ] + left
        diag_dict["diagnosis_code"] = diag_dict["diagnosis_code"] + left
        scored_indices = np.isin(diag_dict["diagnosis_code"], dx_mapping_scored["SNOMEDCTCode"].values)
        diag_scored_dict["diagnosis_code"] = [
            item for idx, item in enumerate(diag_dict["diagnosis_code"]) if scored_indices[idx]
        ]
        diag_scored_dict["diagnosis_abbr"] = [
            item for idx, item in enumerate(diag_dict["diagnosis_abbr"]) if scored_indices[idx]
        ]
        diag_scored_dict["diagnosis_fullname"] = [
            item for idx, item in enumerate(diag_dict["diagnosis_fullname"]) if scored_indices[idx]
        ]
        # except Exception:  # the old version, the Dx's are abbreviations, deprecated
        # diag_dict["diagnosis_abbr"] = diag_dict["diagnosis_code"]
        # selection = dx_mapping_all["Abbreviation"].isin(diag_dict["diagnosis_abbr"])
        # diag_dict["diagnosis_fullname"] = dx_mapping_all[selection]["Dx"].tolist()
        # if not keep_original:
        #     for idx, d in enumerate(ann_dict["diagnosis_abbr"]):
        #         if d in ["Normal", "NSR"]:
        #             ann_dict["diagnosis_abbr"] = ["N"]
        return diag_dict, diag_scored_dict

    def _parse_leads(self, l_leads_data: List[str]) -> pd.DataFrame:
        """Parse leads information from a list of strings.

        Parameters
        ----------
        l_leads_data : List[str]
            Raw information of each lead, read from a header file.

        Returns
        -------
        df_leads : pandas.DataFrame
            Infomation of each leads in the format
            of a :class:`~pandas.DataFrame`.

        """
        df_leads = pd.read_csv(io.StringIO("\n".join(l_leads_data)), sep="\\s+", header=None)
        df_leads.columns = [
            "filename",
            "fmt+byte_offset",
            "adc_gain+units",
            "adc_res",
            "adc_zero",
            "init_value",
            "checksum",
            "block_size",
            "lead_name",
        ]
        df_leads["fmt"] = df_leads["fmt+byte_offset"].apply(lambda s: s.split("+")[0])
        df_leads["byte_offset"] = df_leads["fmt+byte_offset"].apply(lambda s: s.split("+")[1])
        df_leads["adc_gain"] = df_leads["adc_gain+units"].apply(lambda s: s.split("/")[0])
        df_leads["adc_units"] = df_leads["adc_gain+units"].apply(lambda s: s.split("/")[1])
        for k in [
            "byte_offset",
            "adc_gain",
            "adc_res",
            "adc_zero",
            "init_value",
            "checksum",
        ]:
            df_leads[k] = df_leads[k].apply(lambda s: int(s))
        df_leads["baseline"] = df_leads["adc_zero"]
        df_leads = df_leads[
            [
                "filename",
                "fmt",
                "byte_offset",
                "adc_gain",
                "adc_units",
                "adc_res",
                "adc_zero",
                "baseline",
                "init_value",
                "checksum",
                "block_size",
                "lead_name",
            ]
        ]
        df_leads.index = df_leads["lead_name"]
        df_leads.index.name = None
        return df_leads

    @add_docstring(load_ann.__doc__)
    def load_header(self, rec: Union[str, int], raw: bool = False) -> Union[dict, str]:
        """
        alias for `load_ann`, as annotations are also stored in header files
        """
        return self.load_ann(rec, raw)

    def get_labels(
        self,
        rec: Union[str, int],
        scored_only: bool = True,
        fmt: str = "s",
        normalize: bool = True,
    ) -> List[str]:
        """Get labels (diagnoses or arrhythmias) of the record.

        Parameters
        ----------
        rec : str or int
            Record name or index of the record in :attr:`all_records`.
        scored_only : bool, default True
            If True, only get the labels that are scored
            in the CINC2021 official phase.
        fmt : str, default "s"
            Format of labels, one of the following (case insensitive):

                - "a", abbreviations
                - "f", full names
                - "s", SNOMED CT Code

        normalize : bool, default True
            If True, the labels will be transformed into their equavalents,
            which are defined in `utils.utils_misc.cinc2021_aux_data.py`.

        Returns
        -------
        labels : List[str]
            The list of labels of the record.

        """
        if isinstance(rec, int):
            rec = self[rec]
        ann_dict = self.load_ann(rec)
        if scored_only:
            _labels = ann_dict["diagnosis_scored"]
        else:
            _labels = ann_dict["diagnosis"]
        if fmt.lower() == "a":
            _labels = _labels["diagnosis_abbr"]
        elif fmt.lower() == "f":
            _labels = _labels["diagnosis_fullname"]
        elif fmt.lower() == "s":
            _labels = _labels["diagnosis_code"]
        else:
            raise ValueError(f"`fmt` should be one of `a`, `f`, `s`, but got `{fmt}`")
        if normalize:
            # labels = [self.label_trans_dict.get(item, item) for item in labels]
            # remove possible duplicates after normalization
            labels = []
            for item in _labels:
                new_item = self.label_trans_dict.get(item, item)
                if new_item not in labels:
                    labels.append(new_item)
        else:
            labels = _labels
        return labels

    def get_fs(self, rec: Union[str, int], from_hea: bool = True) -> Real:
        """Get the sampling frequency of the record.

        Parameters
        ----------
        rec : str or int
            Record name or index of the record in :attr:`all_records`.
        from_hea : bool, default True
            If True, sampling frequency is read from
            corresponding header file of the record;
            otherwise, `self.fs` is used.

        Returns
        -------
        fs : numbers.Real
            Sampling frequency of the record.

        """
        if from_hea:
            fs = self.load_ann(rec)["fs"]
        else:
            tranche = self._get_tranche(rec)
            fs = self.fs[tranche]
        return fs

    def get_subject_info(self, rec: Union[str, int], items: Optional[List[str]] = None) -> dict:
        """Get auxiliary information of a subject
        (a record) stored in the header files.

        Parameters
        ----------
        rec : str or int
            Record name or index of the record in :attr:`all_records`.
        items : List[str], optional
            Items of the subject's information (e.g. sex, age, etc.).

        Returns
        -------
        subject_info : dict
            Information about the subject, including
            "age", "sex", "medical_prescription",
            "history", "symptom_or_surgery".

        """
        if items is None or len(items) == 0:
            info_items = [
                "age",
                "sex",
                "medical_prescription",
                "history",
                "symptom_or_surgery",
            ]
        else:
            info_items = items
        ann_dict = self.load_ann(rec)
        subject_info = {item: ann_dict[item] for item in info_items}

        return subject_info

    def plot(
        self,
        rec: Union[str, int],
        data: Optional[np.ndarray] = None,
        ann: Optional[Dict[str, Sequence[str]]] = None,
        ticks_granularity: int = 0,
        leads: Optional[Union[str, Sequence[str]]] = None,
        same_range: bool = False,
        waves: Optional[Dict[str, Sequence[int]]] = None,
        **kwargs: Any,
    ) -> None:
        """
        Plot the signals of a record or external signals (units in μV),
        with metadata (fs, labels, tranche, etc.),
        possibly also along with wave delineations.

        Parameters
        ----------
        rec : str or int
            Record name or index of the record in :attr:`all_records`.
        data : numpy.ndarray, optional
            (12-lead) ECG signal to plot,
            should be of the format "channel_first",
            and compatible with `leads`.
            If is not None, data of `rec` will not be used.
            This is useful when plotting filtered data.
        ann : dict, optional
            Annotations for `data`, with 2 items: "scored", "all".
            Ignored if `data` is None.
        ticks_granularity : int, default 0
            Granularity to plot axis ticks, the higher the more ticks.
            0 (no ticks) --> 1 (major ticks) --> 2 (major + minor ticks)
        leads : str or List[str], optional
            The leads of the ECG signal to plot.
        same_range : bool, default False
            If True, all leads are forced to have the same y range.
        waves : dict, optional
            Indices of the wave critical points, including
            "p_onsets", "p_peaks", "p_offsets",
            "q_onsets", "q_peaks", "r_peaks", "s_peaks", "s_offsets",
            "t_onsets", "t_peaks", "t_offsets".
        kwargs : dict, optional
            Additional keyword arguments to pass to :func:`matplotlib.pyplot.plot`.

        TODO
        ----
        1. Slice too long records, and plot separately for each segment.
        2. Plot waves using :func:`matplotlib.pyplot.axvspan`.

        NOTE
        ----
        `Locator` of ``plt`` has default `MAXTICKS` of 1000.
        If not modifying this number, at most 40 seconds of signal could be plotted once.

        Contributors: Jeethan, and WEN Hao

        """
        if isinstance(rec, int):
            rec = self[rec]
        tranche = self._get_tranche(rec)
        if tranche in "CDE":
            physionet_lightwave_suffix = CFG(
                {
                    "C": "incartdb/1.0.0",
                    "D": "ptbdb/1.0.0",
                    "E": "ptb-xl/1.0.1",
                }
            )
            url = f"https://physionet.org/lightwave/?db={physionet_lightwave_suffix[tranche]}"
            self.logger.info(f"better view: {url}")

        if "plt" not in dir():
            import matplotlib.pyplot as plt

            plt.MultipleLocator.MAXTICKS = 3000

        _leads = self._normalize_leads(leads, numeric=False)
        lead_indices = [self.all_leads.index(ld) for ld in _leads]

        if data is None:
            _data = self.load_data(rec, data_format="channel_first", units="μV")[lead_indices]
        else:
            units = self._auto_infer_units(data)
            self.logger.info(f"input data is auto detected to have units in {units}")
            if units.lower() == "mv":
                _data = 1000 * data
            else:
                _data = data
            assert _data.shape[0] == len(
                _leads
            ), f"number of leads from data of shape ({_data.shape[0]}) does not match the length ({len(_leads)}) of `leads`"

        if same_range:
            y_ranges = np.ones((_data.shape[0],)) * np.max(np.abs(_data)) + 100
        else:
            y_ranges = np.max(np.abs(_data), axis=1) + 100

        if waves:
            if waves.get("p_onsets", None) and waves.get("p_offsets", None):
                p_waves = [[onset, offset] for onset, offset in zip(waves["p_onsets"], waves["p_offsets"])]
            elif waves.get("p_peaks", None):
                p_waves = [
                    [
                        max(0, p + ms2samples(_PlotCfg.p_onset, fs=self.get_fs(rec))),
                        min(
                            _data.shape[1],
                            p + ms2samples(_PlotCfg.p_offset, fs=self.get_fs(rec)),
                        ),
                    ]
                    for p in waves["p_peaks"]
                ]
            else:
                p_waves = []
            if waves.get("q_onsets", None) and waves.get("s_offsets", None):
                qrs = [[onset, offset] for onset, offset in zip(waves["q_onsets"], waves["s_offsets"])]
            elif waves.get("q_peaks", None) and waves.get("s_peaks", None):
                qrs = [
                    [
                        max(0, q + ms2samples(_PlotCfg.q_onset, fs=self.get_fs(rec))),
                        min(
                            _data.shape[1],
                            s + ms2samples(_PlotCfg.s_offset, fs=self.get_fs(rec)),
                        ),
                    ]
                    for q, s in zip(waves["q_peaks"], waves["s_peaks"])
                ]
            elif waves.get("r_peaks", None):
                qrs = [
                    [
                        max(0, r + ms2samples(_PlotCfg.qrs_radius, fs=self.get_fs(rec))),
                        min(
                            _data.shape[1],
                            r + ms2samples(_PlotCfg.qrs_radius, fs=self.get_fs(rec)),
                        ),
                    ]
                    for r in waves["r_peaks"]
                ]
            else:
                qrs = []
            if waves.get("t_onsets", None) and waves.get("t_offsets", None):
                t_waves = [[onset, offset] for onset, offset in zip(waves["t_onsets"], waves["t_offsets"])]
            elif waves.get("t_peaks", None):
                t_waves = [
                    [
                        max(0, t + ms2samples(_PlotCfg.t_onset, fs=self.get_fs(rec))),
                        min(
                            _data.shape[1],
                            t + ms2samples(_PlotCfg.t_offset, fs=self.get_fs(rec)),
                        ),
                    ]
                    for t in waves["t_peaks"]
                ]
            else:
                t_waves = []
        else:
            p_waves, qrs, t_waves = [], [], []
        palette = {
            "p_waves": "cyan",
            "qrs": "green",
            "t_waves": "yellow",
        }
        plot_alpha = 0.4

        if ann is None or data is None:
            diag_scored = self.get_labels(rec, scored_only=True, fmt="a")
            diag_all = self.get_labels(rec, scored_only=False, fmt="a")
        else:
            diag_scored = ann["scored"]
            diag_all = ann["all"]

        nb_leads = len(_leads)

        t = np.arange(_data.shape[1]) / self.fs[tranche]
        duration = len(t) / self.fs[tranche]
        fig_sz_w = int(round(DEFAULT_FIG_SIZE_PER_SEC * duration))
        fig_sz_h = 6 * np.maximum(y_ranges, 750) / 1500
        fig, axes = plt.subplots(nb_leads, 1, sharex=False, figsize=(fig_sz_w, np.sum(fig_sz_h)))
        if nb_leads == 1:
            axes = [axes]
        for idx in range(nb_leads):
            axes[idx].plot(
                t,
                _data[idx],
                color="black",
                linewidth="2.0",
                label=f"lead - {_leads[idx]}",
            )
            axes[idx].axhline(y=0, linestyle="-", linewidth="1.0", color="red")
            # NOTE that `Locator` has default `MAXTICKS` equal to 1000
            if ticks_granularity >= 1:
                axes[idx].xaxis.set_major_locator(plt.MultipleLocator(0.2))
                axes[idx].yaxis.set_major_locator(plt.MultipleLocator(500))
                axes[idx].grid(which="major", linestyle="-", linewidth="0.4", color="red")
            if ticks_granularity >= 2:
                axes[idx].xaxis.set_minor_locator(plt.MultipleLocator(0.04))
                axes[idx].yaxis.set_minor_locator(plt.MultipleLocator(100))
                axes[idx].grid(which="minor", linestyle=":", linewidth="0.2", color="gray")
            # add extra info. to legend
            # https://stackoverflow.com/questions/16826711/is-it-possible-to-add-a-string-as-a-legend-item-in-matplotlib
            axes[idx].plot([], [], " ", label=f"labels_s - {','.join(diag_scored)}")
            axes[idx].plot([], [], " ", label=f"labels_a - {','.join(diag_all)}")
            axes[idx].plot([], [], " ", label=f"tranche - {self.tranche_names[tranche]}")
            axes[idx].plot([], [], " ", label=f"fs - {self.fs[tranche]}")
            for w in ["p_waves", "qrs", "t_waves"]:
                for itv in eval(w):
                    axes[idx].axvspan(t[itv[0]], t[itv[1]], color=palette[w], alpha=plot_alpha)
            axes[idx].legend(loc="upper left", fontsize=14)
            axes[idx].set_xlim(t[0], t[-1])
            axes[idx].set_ylim(min(-600, -y_ranges[idx]), max(600, y_ranges[idx]))
            axes[idx].set_xlabel("Time [s]", fontsize=16)
            axes[idx].set_ylabel("Voltage [μV]", fontsize=16)
        plt.subplots_adjust(hspace=0.05)
        fig.tight_layout()
        if kwargs.get("save_path", None):
            plt.savefig(kwargs["save_path"], dpi=200, bbox_inches="tight")
        else:
            plt.show()

    def get_tranche_class_distribution(self, tranches: Sequence[str], scored_only: bool = True) -> Dict[str, int]:
        """Compute class distribution in the tranches.

        Parameters
        ----------
        tranches : Sequence[str]
            Tranche symbols (A-G).
        scored_only : bool, default True
            If True, only classes that are scored in the CINC2021 official phase
            are considered for computing the distribution.

        Returns
        -------
        distribution : dict
            Distribution of classes in the tranches.
            Keys are abbrevations of the classes, and
            values are appearance of corr. classes in the tranche.

        """
        tranche_names = [self.tranche_names[t] for t in tranches]
        df = dx_mapping_scored if scored_only else dx_mapping_all
        distribution = CFG()
        for _, row in df.iterrows():
            num = (row[tranche_names].values).sum()
            if num > 0:
                distribution[row["Abbreviation"]] = num
        return distribution

    def load_resampled_data(
        self,
        rec: Union[str, int],
        leads: Optional[Union[str, List[str]]] = None,
        data_format: str = "channel_first",
        siglen: Optional[int] = None,
    ) -> np.ndarray:
        """
        Resample the data of `rec` to 500Hz,
        or load the resampled data in 500Hz, if the corr. data file already exists

        Parameters
        ----------
        rec : str or int
            Record name or index of the record in :attr:`all_records`.
        leads : str or List[str], optional
            The leads of the ECG data to be loaded.
        data_format : str, default "channel_first"
            Format of the ECG data,
            "channel_last" (alias "lead_last"), or
            "channel_first" (alias "lead_first").
        siglen : int, optional
            Signal length, with units in number of samples.
            If is not None, signal with length longer will be
            sliced to the length of `siglen`.
            Used for preparing/doing model training for example.

        Returns
        -------
        numpy.ndarray
            2D resampled (and perhaps sliced 3D) signal data.

        """
        if isinstance(rec, int):
            rec = self[rec]

        _leads = self._normalize_leads(leads, numeric=True)

        tranche = self._get_tranche(rec)
        if siglen is None:
            rec_fp = self.db_dir / f"{self.db_name}-rsmp-500Hz" / self.tranche_names[tranche] / f"{rec}_500Hz.npy"
        else:
            rec_fp = (
                self.db_dir / f"{self.db_name}-rsmp-500Hz" / self.tranche_names[tranche] / f"{rec}_500Hz_siglen_{siglen}.npy"
            )
        rec_fp.parent.mkdir(parents=True, exist_ok=True)
        if not rec_fp.is_file():
            # self.logger.info(f"corresponding file {rec_fp.name} does not exist")
            # NOTE: if not exists, create the data file,
            # so that the ordering of leads keeps in accordance with `EAK.Standard12Leads`
            data = self.load_data(rec, leads="all", data_format="channel_first", units="mV", fs=None)
            rec_fs = self.get_fs(rec, from_hea=True)
            if rec_fs != 500:
                data = SS.resample_poly(data, 500, rec_fs, axis=1).astype(DEFAULTS.DTYPE.NP)
            # if self.fs[tranche] != 500:
            #     data = SS.resample_poly(data, 500, self.fs[tranche], axis=1)
            if siglen is not None and data.shape[1] >= siglen:
                # slice_start = (data.shape[1] - siglen)//2
                # slice_end = slice_start + siglen
                # data = data[..., slice_start:slice_end]
                data = ensure_siglen(data, siglen=siglen, fmt="channel_first", tolerance=0.2).astype(DEFAULTS.DTYPE.NP)
                np.save(rec_fp, data)
            elif siglen is None:
                np.save(rec_fp, data)
        else:
            # self.logger.info(f"loading from local file...")
            data = np.load(rec_fp).astype(DEFAULTS.DTYPE.NP)
        # choose data of specific leads
        if siglen is None:
            data = data[_leads, ...]
        else:
            data = data[:, _leads, :]
        if data_format.lower() in ["channel_last", "lead_last"]:
            data = np.moveaxis(data, -1, -2)
        return data

    def load_raw_data(self, rec: Union[str, int], backend: Literal["wfdb", "scipy"] = "scipy") -> np.ndarray:
        """Load raw data from corresponding files with no further processing.

        This method facilitates feeding data into the `run_12ECG_classifier` function.

        Parameters
        ----------
        rec : str or int
            Record name or index of the record in :attr:`all_records`.
        backend : {"scipy", "wfdb"}, default "scipy"
            The backend data reader.
            Note that "scipy" provides data in the format of "lead_first",
            while "wfdb" provides data in the format of "lead_last".

        Returns
        -------
        raw_data: numpy.ndarray
            Raw data (d_signal) loaded from corresponding data file,
            without digital-to-analog conversion (DAC) and resampling.

        """
        if isinstance(rec, int):
            rec = self[rec]
        # tranche = self._get_tranche(rec)
        if backend.lower() == "wfdb":
            rec_fp = self.get_data_filepath(rec, with_ext=False)
            wfdb_rec = wfdb.rdrecord(str(rec_fp), physical=False)
            raw_data = np.asarray(wfdb_rec.d_signal, dtype=DEFAULTS.DTYPE.NP)
        elif backend.lower() == "scipy":
            rec_fp = self.get_data_filepath(rec, with_ext=True)
            raw_data = loadmat(str(rec_fp))["val"].astype(DEFAULTS.DTYPE.NP)
        return raw_data

    def _check_exceptions(
        self,
        tranches: Optional[Union[str, Sequence[str]]] = None,
        flat_granularity: str = "record",
    ) -> List[str]:
        """Check if records from `tranches` has nan values,
        or contains constant values in any lead.

        Accessing data using `p_signal` of `wfdb` would produce nan values,
        if exceptionally large values are encountered.
        Tthis could help detect abnormal records as well.

        Parameters
        ----------
        tranches : str or Sequence[str], optional
            Tranches to check, defaults to all tranches,
            i.e. `self.db_tranches`.
        flat_granularity : str, default "record"
            Granularity of flat (constant value) checking.
            If is "record", flat checking will only be carried out at record level;
            if is "lead", flat checking will be carried out at lead level.

        Returns
        -------
        exceptional_records : List[str]
            List of exceptional records.

        """
        six_leads = ("I", "II", "III", "aVR", "aVL", "aVF")
        four_leads = ("I", "II", "III", "V2")
        three_leads = ("I", "II", "V2")
        two_leads = ("I", "II")

        exceptional_records = []
        _two_leads = set(two_leads)
        _three_leads = set(three_leads)
        _four_leads = set(four_leads)
        _six_leads = set(six_leads)
        for t in tranches or self.db_tranches:
            for rec in self.all_records[t]:
                data = self.load_data(rec)
                if np.isnan(data).any():
                    self.logger.info(f"record {rec} from tranche {t} has nan values")
                elif np.std(data) == 0:
                    self.logger.info(f"record {rec} from tranche {t} is flat")
                elif (np.std(data, axis=1) == 0).any():
                    exceptional_leads = set(np.array(self.all_leads)[np.where(np.std(data, axis=1) == 0)[0]].tolist())
                    cond = any(
                        [
                            _two_leads.issubset(exceptional_leads),
                            _three_leads.issubset(exceptional_leads),
                            _four_leads.issubset(exceptional_leads),
                            _six_leads.issubset(exceptional_leads),
                        ]
                    )
                    if cond or flat_granularity.lower() == "lead":
                        self.logger.info(f"leads {exceptional_leads} of record {rec} from tranche {t} is flat")
                    else:
                        continue
                else:
                    continue
                exceptional_records.append(rec)
        return exceptional_records

    def _compute_cooccurrence(self, tranches: Optional[str] = None) -> pd.DataFrame:
        """Compute the coocurrence matrix (:class:`~pandas.DataFrame`)
        of all classes in the whole of the CinC2021 database.

        Parameters
        ----------
        tranches : str, optional
            Tranches to compute coocurrence matrix, defaults to all tranches.
            If specified, computation will be limited to these tranches,
            e.g. "AB", "ABEF", "G", etc., case insensitive.

        Returns
        -------
        dx_cooccurrence_all : pandas.DataFrame
            The coocurrence matrix of the classes in given tranches.

        """
        if dx_cooccurrence_all_fp.is_file() and tranches is None:
            dx_cooccurrence_all = pd.read_csv(dx_cooccurrence_all_fp, index_col=0)
            if not dx_cooccurrence_all.empty:
                return dx_cooccurrence_all
        dx_cooccurrence_all = pd.DataFrame(
            np.zeros(
                (len(dx_mapping_all.Abbreviation), len(dx_mapping_all.Abbreviation)),
                dtype=int,
            ),
            columns=dx_mapping_all.Abbreviation.values,
        )
        dx_cooccurrence_all.index = dx_mapping_all.Abbreviation.values
        start = time.time()
        self.logger.info("start computing the cooccurrence matrix...")
        _tranches = (tranches or "").upper() or list(self.all_records.keys())
        for tranche, l_rec in self.all_records.items():
            if tranche not in _tranches:
                continue
            for idx, rec in enumerate(l_rec):
                ann = self.load_ann(rec)
                d = ann["diagnosis"]["diagnosis_abbr"]
                for item in d:
                    if item not in dx_cooccurrence_all.columns.values:
                        # ref. ISSUE 7
                        # self.logger.info(f"{rec} has illegal Dx {item}!")
                        continue
                    dx_cooccurrence_all.loc[item, item] += 1
                for i in range(len(d) - 1):
                    if d[i] not in dx_cooccurrence_all.columns.values:
                        continue
                    for j in range(i + 1, len(d)):
                        if d[j] not in dx_cooccurrence_all.columns.values:
                            continue
                        dx_cooccurrence_all.loc[d[i], d[j]] += 1
                        dx_cooccurrence_all.loc[d[j], d[i]] += 1
                print(f"tranche {tranche} <-- {idx+1} / {len(l_rec)}", end="\r")
        self.logger.info(f"finish computing the cooccurrence matrix in {(time.time()-start)/60:.3f} minutes")
        if tranches is None:
            dx_cooccurrence_all.to_csv(dx_cooccurrence_all_fp)
        return dx_cooccurrence_all

    @property
    def url(self) -> List[str]:
        domain = "https://storage.cloud.google.com/physionetchallenge2021-public-datasets/"
        return [posixpath.join(domain, f) for f in self.data_files]

    data_files = [
        "WFDB_CPSC2018.tar.gz",
        "WFDB_CPSC2018_2.tar.gz",
        "WFDB_StPetersburg.tar.gz",
        "WFDB_PTB.tar.gz",
        "WFDB_PTBXL.tar.gz",
        "WFDB_Ga.tar.gz",
        # "WFDB_ShaoxingUniv.tar.gz",
        "WFDB_ChapmanShaoxing.tar.gz",
        "WFDB_Ningbo.tar.gz",
    ]

    header_files = [
        "CPSC2018-Headers.tar.gz",
        "CPSC2018-2-Headers.tar.gz",
        "StPetersburg-Headers.tar.gz",
        "PTB-Headers.tar.gz",
        "PTB-XL-Headers.tar.gz",
        "Ga-Headers.tar.gz",
        # "ShaoxingUniv_Headers.tar.gz",
        "ChapmanShaoxing-Headers.tar.gz",
        "Ningbo-Headers.tar.gz",
    ]

    def download(self) -> None:
        for url in self.url:
            http_get(url, self.db_dir_base, extract=True)
        self._ls_rec()

    def __len__(self) -> int:
        return len(self.__all_records)

    def __getitem__(self, index: int) -> str:
        return self.__all_records[index]

    @property
    def database_info(self) -> DataBaseInfo:
        return _CINC2021_INFO


# fmt: off
_exceptional_records = [  # with nan values (p_signal) read by wfdb
    "I0002", "I0069",
    "E04603", "E06072", "E06909", "E07675", "E07941", "E08321",
    "JS10765", "JS10767", "JS10890", "JS10951", "JS11887", "JS11897",
    "JS11956", "JS12751", "JS13181", "JS14161", "JS14343", "JS14627",
    "JS14659", "JS15624", "JS16169", "JS16222", "JS16813", "JS19309",
    "JS19708", "JS20330", "JS20656", "JS21144", "JS21617", "JS21668",
    "JS21701", "JS21853", "JS21881", "JS23116", "JS23450", "JS23482",
    "JS23588", "JS23786", "JS23950", "JS24016", "JS25106", "JS25322",
    "JS25458", "JS26009", "JS26130", "JS26145", "JS26245", "JS26605",
    "JS26793", "JS26843", "JS26977", "JS27034", "JS27170", "JS27271",
    "JS27278", "JS27407", "JS27460", "JS27835", "JS27985", "JS28075",
    "JS28648", "JS28757", "JS33280", "JS34479", "JS34509", "JS34788",
    "JS34868", "JS34879", "JS35050", "JS35065", "JS35192", "JS35654",
    "JS35727", "JS36015", "JS36018", "JS36189", "JS36244", "JS36568",
    "JS36731", "JS37105", "JS37173", "JS37176", "JS37439", "JS37592",
    "JS37609", "JS37781", "JS38231", "JS38252", "JS41844", "JS41908",
    "JS41935", "JS42026", "JS42330",
    # with totally flat values
    "Q0400",
    "Q2961",
]
# fmt: on


def compute_all_metrics_detailed(
    classes: List[str], truth: Sequence, binary_pred: Sequence, scalar_pred: Sequence
) -> Tuple[Union[float, np.ndarray]]:
    """Compute detailed metrics for each class.

    Parameters
    ----------
    classes : List[str]
        List of all the classes, in the format of abbrevations.
    truth : array_like
        Ground truth array, of shape ``(n_records, n_classes)``,
        with values 0 or 1.
    binary_pred : array_like
        Binary predictions, of shape ``(n_records, n_classes)``,
        with values 0 or 1.
    scalar_pred : array_like
        Probability predictions, of shape ``(n_records, n_classes)``,
        with values within the interval [0, 1].

    Returns
    -------
    auroc : float
        Area under the receiver operating characteristic (ROC) curve.
    auprc : float
        Area under the precision-recall curve.
    accuracy : float
        Macro-averaged accuracy.
    f_measure : float
        Macro-averaged F1 score.
    f_measure_classes : numpy.ndarray
        F1 score for each class.
    f_beta_measure : float
        Macro-averaged F-beta score.
    g_beta_measure : float
        Macro-averaged G-beta score.
    challenge_metric : float
        Challenge metric, defined by a weight matrix.

    """
    # sinus_rhythm = "426783006"
    sinus_rhythm = "NSR"
    weights = load_weights(classes=classes)

    _truth = np.array(truth)
    _binary_pred = np.array(binary_pred)
    _scalar_pred = np.array(scalar_pred)

    print("- AUROC and AUPRC...")
    auroc, auprc, auroc_classes, auprc_classes = _compute_auc(_truth, _scalar_pred)

    print("- Accuracy...")
    accuracy = _compute_accuracy(_truth, _binary_pred)

    print("- F-measure...")
    f_measure, f_measure_classes = _compute_f_measure(_truth, _binary_pred)

    print("- F-beta and G-beta measures...")
    # NOTE that F-beta and G-beta are not among metrics of CinC2021, in contrast to CinC2020
    f_beta_measure, g_beta_measure = _compute_beta_measures(_truth, _binary_pred, beta=2)

    print("- Challenge metric...")
    challenge_metric = compute_challenge_metric(weights, _truth, _binary_pred, classes, sinus_rhythm)

    print("Done.")

    # Return the results.
    ret_tuple = (
        auroc,
        auprc,
        auroc_classes,
        auprc_classes,
        accuracy,
        f_measure,
        f_measure_classes,
        f_beta_measure,
        g_beta_measure,
        challenge_metric,
    )
    return ret_tuple


def compute_all_metrics(
    classes: List[str], truth: Sequence, binary_pred: Sequence, scalar_pred: Sequence
) -> Tuple[Union[float, np.ndarray]]:
    """Simplified version of :func:`compute_all_metrics_detailed`.

    This function doesnot produce per-class scores.

    Parameters
    ----------
    classes : List[str]
        List of all the classes, in the format of abbrevations.
    truth : array_like
        Ground truth array, of shape ``(n_records, n_classes)``,
        with values 0 or 1.
    binary_pred : array_like
        Binary predictions, of shape ``(n_records, n_classes)``,
        with values 0 or 1.
    scalar_pred : array_like
        Probability predictions, of shape ``(n_records, n_classes)``,
        with values within the interval [0, 1].

    Returns
    -------
    auroc : float
        Area under the receiver operating characteristic (ROC) curve
    auprc : float
        Area under the precision-recall curve.
    accuracy : float
        Macro-averaged accuracy.
    f_measure : float
        Macro-averaged F1 score.
    f_beta_measure : float
        Macro-averaged F-beta score.
    g_beta_measure : float
        Macro-averaged G-beta score.
    challenge_metric : float
        challenge metric, defined by a weight matrix

    """
    (
        auroc,
        auprc,
        _,
        _,
        accuracy,
        f_measure,
        _,
        f_beta_measure,
        g_beta_measure,
        challenge_metric,
    ) = compute_all_metrics_detailed(classes, truth, binary_pred, scalar_pred)
    return (
        auroc,
        auprc,
        accuracy,
        f_measure,
        f_beta_measure,
        g_beta_measure,
        challenge_metric,
    )


def _compute_accuracy(labels: np.ndarray, outputs: np.ndarray) -> float:
    """Compute recording-wise accuracy.

    Parameters
    ----------
    labels : numpy.ndarray
        Ground truth array, of shape ``(n_records, n_classes)``,
        with values 0 or 1.
    outputs : numpy.ndarray
        Binary predictions, of shape ``(n_records, n_classes)``,
        with values 0 or 1.

    Returns
    -------
    accuracy : float
        Macro-averaged accuracy.

    """
    num_recordings, num_classes = np.shape(labels)

    num_correct_recordings = 0
    for i in range(num_recordings):
        if np.all(labels[i, :] == outputs[i, :]):
            num_correct_recordings += 1

    return float(num_correct_recordings) / float(num_recordings)


def _compute_confusion_matrices(labels: np.ndarray, outputs: np.ndarray, normalize: bool = False) -> np.ndarray:
    """Compute confusion matrices.

    Compute a binary confusion matrix for each class k:

          [TN_k FN_k]
          [FP_k TP_k]

    If the normalize variable is set to true, then normalize the contributions
    to the confusion matrix by the number of labels per recording.

    Parameters
    ----------
    labels : numpy.ndarray
        Ground truth array, of shape ``(n_records, n_classes)``,
        with values 0 or 1.
    outputs : numpy.ndarray
        Binary predictions, of shape ``(n_records, n_classes)``,
        with values 0 or 1.
    normalize : bool, optional
        If true, normalize the confusion matrices by the number of labels per
        recording. Default is false.

    Returns
    -------
    A : numpy.ndarray
        Confusion matrices, of shape ``(n_classes, 2, 2)``.

    """
    num_recordings, num_classes = np.shape(labels)

    if not normalize:
        A = np.zeros((num_classes, 2, 2))
        for i in range(num_recordings):
            for j in range(num_classes):
                if labels[i, j] == 1 and outputs[i, j] == 1:  # TP
                    A[j, 1, 1] += 1
                elif labels[i, j] == 0 and outputs[i, j] == 1:  # FP
                    A[j, 1, 0] += 1
                elif labels[i, j] == 1 and outputs[i, j] == 0:  # FN
                    A[j, 0, 1] += 1
                elif labels[i, j] == 0 and outputs[i, j] == 0:  # TN
                    A[j, 0, 0] += 1
                else:  # This condition should not happen.
                    raise ValueError("Error in computing the confusion matrix.")
    else:
        A = np.zeros((num_classes, 2, 2))
        for i in range(num_recordings):
            normalization = float(max(np.sum(labels[i, :]), 1))
            for j in range(num_classes):
                if labels[i, j] == 1 and outputs[i, j] == 1:  # TP
                    A[j, 1, 1] += 1.0 / normalization
                elif labels[i, j] == 0 and outputs[i, j] == 1:  # FP
                    A[j, 1, 0] += 1.0 / normalization
                elif labels[i, j] == 1 and outputs[i, j] == 0:  # FN
                    A[j, 0, 1] += 1.0 / normalization
                elif labels[i, j] == 0 and outputs[i, j] == 0:  # TN
                    A[j, 0, 0] += 1.0 / normalization
                else:  # This condition should not happen.
                    raise ValueError("Error in computing the confusion matrix.")

    return A


def _compute_f_measure(labels: np.ndarray, outputs: np.ndarray) -> Tuple[float, np.ndarray]:
    """Compute macro-averaged F1 score, and F1 score per class.

    Parameters
    ----------
    labels : numpy.ndarray
        Ground truth array, of shape ``(n_records, n_classes)``,
        with values 0 or 1.
    outputs : numpy.ndarray
        Binary predictions, of shape ``(n_records, n_classes)``,
        with values 0 or 1.

    Returns
    -------
    macro_f_measure : float
        Macro-averaged F1 score.
    f_measure : numpy.ndarray
        F1 score per class, of shape ``(n_classes,)``.

    """
    num_recordings, num_classes = np.shape(labels)

    A = _compute_confusion_matrices(labels, outputs)

    f_measure = np.zeros(num_classes)
    for k in range(num_classes):
        tp, fp, fn, tn = A[k, 1, 1], A[k, 1, 0], A[k, 0, 1], A[k, 0, 0]
        if 2 * tp + fp + fn:
            f_measure[k] = float(2 * tp) / float(2 * tp + fp + fn)
        else:
            f_measure[k] = float("nan")

    if np.any(np.isfinite(f_measure)):
        macro_f_measure = np.nanmean(f_measure)
    else:
        macro_f_measure = float("nan")

    return macro_f_measure, f_measure


def _compute_beta_measures(labels: np.ndarray, outputs: np.ndarray, beta: Real) -> Tuple[float, float]:
    """Compute F-beta and G-beta measures.

    Parameters
    ----------
    labels : numpy.ndarray
        Ground truth array, of shape ``(n_records, n_classes)``,
        with values 0 or 1.
    outputs : numpy.ndarray
        Binary predictions, of shape ``(n_records, n_classes)``,
        with values 0 or 1.
    beta : float
        Beta parameter.

    Returns
    -------
    f_beta_measure : float
        Macro-averaged F-beta measure.
    g_beta_measure : float
        Macro-averaged G-beta measure.

    """
    num_recordings, num_classes = np.shape(labels)

    A = _compute_confusion_matrices(labels, outputs, normalize=True)

    f_beta_measure = np.zeros(num_classes)
    g_beta_measure = np.zeros(num_classes)
    for k in range(num_classes):
        tp, fp, fn, tn = A[k, 1, 1], A[k, 1, 0], A[k, 0, 1], A[k, 0, 0]
        if (1 + beta**2) * tp + fp + beta**2 * fn:
            f_beta_measure[k] = float((1 + beta**2) * tp) / float((1 + beta**2) * tp + fp + beta**2 * fn)
        else:
            f_beta_measure[k] = float("nan")
        if tp + fp + beta * fn:
            g_beta_measure[k] = float(tp) / float(tp + fp + beta * fn)
        else:
            g_beta_measure[k] = float("nan")

    macro_f_beta_measure = np.nanmean(f_beta_measure)
    macro_g_beta_measure = np.nanmean(g_beta_measure)

    return macro_f_beta_measure, macro_g_beta_measure


def _compute_auc(labels: np.ndarray, outputs: np.ndarray) -> Tuple[float, float, np.ndarray, np.ndarray]:
    """Compute macro-averaged AUROC and macro-averaged AUPRC.

    Parameters
    ----------
    labels : numpy.ndarray
        Ground truth array, of shape ``(n_records, n_classes)``,
        with values 0 or 1.
    outputs : numpy.ndarray
        Binary predictions, of shape ``(n_records, n_classes)``,
        with values 0 or 1.

    Returns
    -------
    macro_auroc : float
        Macro-averaged AUROC.
    macro_auprc : float
        Macro-averaged AUPRC.
    auroc : numpy.ndarray
        AUROC per class, of shape ``(n_classes,)``.
    auprc : numpy.ndarray
        AUPRC per class, of shape ``(n_classes,)``.

    """
    num_recordings, num_classes = np.shape(labels)

    # Compute and summarize the confusion matrices for each class across at distinct output values.
    auroc = np.zeros(num_classes)
    auprc = np.zeros(num_classes)

    for k in range(num_classes):
        # We only need to compute TPs, FPs, FNs, and TNs at distinct output values.
        thresholds = np.unique(outputs[:, k])
        thresholds = np.append(thresholds, thresholds[-1] + 1)
        thresholds = thresholds[::-1]
        num_thresholds = len(thresholds)

        # Initialize the TPs, FPs, FNs, and TNs.
        tp = np.zeros(num_thresholds)
        fp = np.zeros(num_thresholds)
        fn = np.zeros(num_thresholds)
        tn = np.zeros(num_thresholds)
        fn[0] = np.sum(labels[:, k] == 1)
        tn[0] = np.sum(labels[:, k] == 0)

        # Find the indices that result in sorted output values.
        idx = np.argsort(outputs[:, k])[::-1]

        # Compute the TPs, FPs, FNs, and TNs for class k across thresholds.
        i = 0
        for j in range(1, num_thresholds):
            # Initialize TPs, FPs, FNs, and TNs using values at previous threshold.
            tp[j] = tp[j - 1]
            fp[j] = fp[j - 1]
            fn[j] = fn[j - 1]
            tn[j] = tn[j - 1]

            # Update the TPs, FPs, FNs, and TNs at i-th output value.
            while i < num_recordings and outputs[idx[i], k] >= thresholds[j]:
                if labels[idx[i], k]:
                    tp[j] += 1
                    fn[j] -= 1
                else:
                    fp[j] += 1
                    tn[j] -= 1
                i += 1

        # Summarize the TPs, FPs, FNs, and TNs for class k.
        tpr = np.zeros(num_thresholds)
        tnr = np.zeros(num_thresholds)
        ppv = np.zeros(num_thresholds)
        for j in range(num_thresholds):
            if tp[j] + fn[j]:
                tpr[j] = float(tp[j]) / float(tp[j] + fn[j])
            else:
                tpr[j] = float("nan")
            if fp[j] + tn[j]:
                tnr[j] = float(tn[j]) / float(fp[j] + tn[j])
            else:
                tnr[j] = float("nan")
            if tp[j] + fp[j]:
                ppv[j] = float(tp[j]) / float(tp[j] + fp[j])
            else:
                ppv[j] = float("nan")

        # Compute AUROC as the area under a piecewise linear function with TPR/
        # sensitivity (x-axis) and TNR/specificity (y-axis) and AUPRC as the area
        # under a piecewise constant with TPR/recall (x-axis) and PPV/precision
        # (y-axis) for class k.
        for j in range(num_thresholds - 1):
            auroc[k] += 0.5 * (tpr[j + 1] - tpr[j]) * (tnr[j + 1] + tnr[j])
            auprc[k] += (tpr[j + 1] - tpr[j]) * ppv[j + 1]

    # Compute macro AUROC and macro AUPRC across classes.
    if np.any(np.isfinite(auroc)):
        macro_auroc = np.nanmean(auroc)
    else:
        macro_auroc = float("nan")
    if np.any(np.isfinite(auprc)):
        macro_auprc = np.nanmean(auprc)
    else:
        macro_auprc = float("nan")

    return macro_auroc, macro_auprc, auroc, auprc


# Compute modified confusion matrix for multi-class, multi-label tasks.
def _compute_modified_confusion_matrix(labels: np.ndarray, outputs: np.ndarray) -> np.ndarray:
    """
    Compute a binary multi-class, multi-label confusion matrix,
    where the rows are the labels and the columns are the outputs.

    Parameters
    ----------
    labels : numpy.ndarray
        Ground truth array, of shape ``(n_records, n_classes)``,
        with values 0 or 1.
    outputs : numpy.ndarray
        Binary predictions, of shape ``(n_records, n_classes)``,
        with values 0 or 1.

    Returns
    -------
    A : numpy.ndarray
        Modified confusion matrix, of shape ``(n_classes, n_classes)``.

    """
    num_recordings, num_classes = np.shape(labels)
    A = np.zeros((num_classes, num_classes))

    # Iterate over all of the recordings.
    for i in range(num_recordings):
        # Calculate the number of positive labels and/or outputs.
        normalization = float(max(np.sum(np.any((labels[i, :], outputs[i, :]), axis=0)), 1))
        # Iterate over all of the classes.
        for j in range(num_classes):
            # Assign full and/or partial credit for each positive class.
            if labels[i, j]:
                for k in range(num_classes):
                    if outputs[i, k]:
                        A[j, k] += 1.0 / normalization

    return A


# Compute the evaluation metric for the Challenge.
def compute_challenge_metric(
    weights: np.ndarray,
    labels: np.ndarray,
    outputs: np.ndarray,
    classes: List[str],
    sinus_rhythm: str,
) -> float:
    """Compute the evaluation metrics for the Challenge.

    Parameters
    ----------
    weights : numpy.ndarray
        Array of weights, of shape ``(n_classes, n_classes)``.
    labels : numpy.ndarray
        Ground truth array, of shape ``(n_records, n_classes)``,
        with values 0 or 1.
    outputs : numpy.ndarray
        Binary predictions, of shape ``(n_records, n_classes)``,
        with values 0 or 1.
    classes : List[str]
        List of class names.
    sinus_rhythm : str
        Name of the sinus rhythm class.

    Returns
    -------
    score : float
        Challenge metric score.

    """
    num_recordings, num_classes = np.shape(labels)
    if sinus_rhythm in classes:
        sinus_rhythm_index = classes.index(sinus_rhythm)
    else:
        raise ValueError("The sinus rhythm class is not available.")

    # Compute the observed score.
    A = _compute_modified_confusion_matrix(labels, outputs)
    observed_score = np.nansum(weights * A)

    # Compute the score for the model that always chooses the correct label(s).
    correct_outputs = labels
    A = _compute_modified_confusion_matrix(labels, correct_outputs)
    correct_score = np.nansum(weights * A)

    # Compute the score for the model that always chooses the sinus rhythm class.
    inactive_outputs = np.zeros((num_recordings, num_classes), dtype=bool)
    inactive_outputs[:, sinus_rhythm_index] = 1
    A = _compute_modified_confusion_matrix(labels, inactive_outputs)
    inactive_score = np.nansum(weights * A)

    if correct_score != inactive_score:
        normalized_score = float(observed_score - inactive_score) / float(correct_score - inactive_score)
    else:
        normalized_score = 0.0

    return normalized_score


# alias
compute_metrics = compute_all_metrics
compute_metrics_detailed = compute_all_metrics_detailed
