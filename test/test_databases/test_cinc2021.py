"""
TestCINC2021: accomplished
TestCINC2021Dataset: accomplished

subsampling: accomplished
"""

import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from torch_ecg.cfg import DEFAULTS
from torch_ecg.databases import CINC2021, DataBaseInfo
from torch_ecg.databases.aux_data.cinc2021_aux_data import (
    dx_mapping_scored,
    get_class,
    get_class_count,
    get_class_weight,
    get_cooccurrence,
    load_weights,
)
from torch_ecg.databases.datasets import CINC2021Dataset, CINC2021TrainCfg
from torch_ecg.databases.datasets.cinc2021.cinc2021_cfg import four_leads, six_leads, three_leads, twelve_leads, two_leads
from torch_ecg.databases.physionet_databases.cinc2021 import compute_metrics, compute_metrics_detailed
from torch_ecg.utils import dicts_equal

###############################################################################
# set paths
_CWD = Path(__file__).absolute().parents[2] / "sample-data" / "cinc2021"
###############################################################################


reader = CINC2021(_CWD)


class TestCINC2021:
    def test_len(self):
        assert len(reader) == 50
        for db in list("ABCD"):
            assert len(reader.all_records[db]) == 0
        assert len(reader.all_records["E"]) == 10
        assert len(reader.all_records["F"]) == 20
        assert len(reader.all_records["G"]) == 20

    def test_subsample(self):
        ss_ratio = 0.3
        reader_ss = CINC2021(_CWD, subsample=ss_ratio, verbose=0)
        assert len(reader_ss) == pytest.approx(len(reader) * ss_ratio, abs=1)
        ss_ratio = 0.1 / len(reader)
        reader_ss = CINC2021(_CWD, subsample=ss_ratio)
        assert len(reader_ss) == 1

        with pytest.raises(AssertionError, match="`subsample` must be in \\(0, 1\\], but got `.+`"):
            CINC2021(_CWD, subsample=0.0)
        with pytest.raises(AssertionError, match="`subsample` must be in \\(0, 1\\], but got `.+`"):
            CINC2021(_CWD, subsample=1.01)
        with pytest.raises(AssertionError, match="`subsample` must be in \\(0, 1\\], but got `.+`"):
            CINC2021(_CWD, subsample=-0.1)

    def test_load_data(self):
        for rec in reader:
            data = reader.load_data(rec)
            data_1 = reader.load_data(rec, leads=[1, 7])
            assert data.shape[0] == 12
            assert data_1.shape[0] == 2
            assert np.allclose(data[[1, 7], :], data_1)
            data_1 = reader.load_data(rec, units="uV")
            assert np.allclose(data_1, data * 1000)
            data_1 = reader.load_data(rec, units=None)
            assert data.shape == data_1.shape
            data_1 = reader.load_data(rec, data_format="lead_last")
            assert data.shape == data_1.T.shape
            data_1 = reader.load_data(rec, fs=2 * reader.get_fs(rec))
            assert data_1.shape[1] == 2 * data.shape[1]
            data_1 = reader.load_data(rec, backend="scipy")
            assert np.allclose(data_1, data)
            data_1, data_1_fs = reader.load_data(rec, fs=300, return_fs=True)
            assert data_1_fs == 300

        reader.load_data(0, leads=2)
        reader.load_data(0, leads="aVR")

        with pytest.raises(AssertionError, match="Invalid data_format: `flat`"):
            reader.load_data(0, data_format="flat")
        with pytest.raises(ValueError, match="backend `numpy` not supported for loading data"):
            reader.load_data(0, backend="numpy")

    def test_load_ann(self):
        for rec in reader:
            ann_1 = reader.load_ann(rec)
            ann_3 = reader.load_ann(rec, raw=True)
            assert isinstance(ann_1, dict)
            assert isinstance(ann_3, str)
        ann_1 = reader.load_ann(0)
        ann_3 = reader.load_ann(0, raw=True)
        assert isinstance(ann_1, dict)
        assert isinstance(ann_3, str)

    def test_load_header(self):
        # alias for `load_ann`
        for rec in reader:
            header = reader.load_header(rec)
            assert dicts_equal(header, reader.load_ann(rec))
        reader.load_header(0)

    def test_get_labels(self):
        for rec in reader:
            labels_1 = reader.get_labels(rec)
            labels_2 = reader.get_labels(rec, fmt="f")
            labels_3 = reader.get_labels(rec, fmt="a", normalize=False)
            labels_4 = reader.get_labels(rec, scored_only=False)
            assert len(labels_1) == len(labels_2) == len(labels_3) <= len(labels_4)
            assert set(labels_1) <= set(labels_4)
        with pytest.raises(ValueError, match="`fmt` should be one of `a`, `f`, `s`, but got `.+`"):
            reader.get_labels(0, fmt="xxx")

    def test_get_fs(self):
        for rec in reader:
            assert reader.get_fs(rec) in reader.fs.values()
        assert isinstance(reader.get_fs(0, from_hea=False), int)

    def test_get_subject_id(self):
        for rec in reader:
            assert isinstance(reader.get_subject_id(rec), int)
        assert isinstance(reader.get_subject_id(0), int)

    def test_get_subject_info(self):
        for rec in reader:
            info = reader.get_subject_info(rec)
            assert isinstance(info, dict)
            assert info.keys() == {
                "age",
                "sex",
                "medical_prescription",
                "history",
                "symptom_or_surgery",
            }
            info_1 = reader.get_subject_info(rec, items=["age", "sex"])
            assert info_1.keys() <= info.keys()
            for k, v in info_1.items():
                assert info[k] == v
        reader.get_subject_info(0)

    def test_get_tranche_class_distribution(self):
        dist = reader.get_tranche_class_distribution(list("ABCDE"))
        assert isinstance(dist, dict)
        dist_1 = reader.get_tranche_class_distribution(list("ABCDE"), scored_only=False)
        assert isinstance(dist_1, dict)
        assert set(dist.keys()) <= set(dist_1.keys())
        for k, v in dist.items():
            assert v == dist_1[k]

    def test_load_resampled_data(self):
        for rec in reader:
            data = reader.load_resampled_data(rec)
            assert data.ndim == 2 and data.shape[0] == 12
            data_1 = reader.load_resampled_data(rec, data_format="lead_last")
            assert np.allclose(data, data_1.T)
            data_1 = reader.load_resampled_data(rec, siglen=2000)
            assert data_1.ndim == 3 and data_1.shape[1:] == (12, 2000)
        reader.load_resampled_data(0)

    def test_load_raw_data(self):
        for rec in reader:
            data_1 = reader.load_raw_data(rec, backend="wfdb")  # lead-last
            data_2 = reader.load_raw_data(rec, backend="scipy")  # lead-first
            assert data_1.ndim == 2 and data_1.shape[1] == 12
            assert data_2.ndim == 2 and data_2.shape[0] == 12
            assert np.allclose(data_1, data_2.T)
        reader.load_raw_data(0)

    def test_meta_data(self):
        assert isinstance(reader.webpage, str) and len(reader.webpage) > 0
        assert isinstance(reader.url, list) and len(reader.url) - 1 == len(reader.all_records) == len(
            reader.tranche_names
        ) == len(reader.db_tranches)
        assert reader.get_citation() is None  # printed
        with pytest.warns(
            RuntimeWarning,
            match="the dataframe of stats is empty, try using _aggregate_stats",
        ):
            assert reader.df_stats.empty
        assert set(reader.diagnoses_records_list.keys()) >= set(dx_mapping_scored.Abbreviation)
        assert not reader.df_stats.empty
        assert set(reader._check_exceptions()) <= set(reader.exceptional_records)
        df_1 = reader._compute_cooccurrence(tranches="F")
        df_2 = reader._compute_cooccurrence(tranches="FG")
        assert df_2.shape == df_1.shape
        assert (df_1 <= df_2).all(None)
        assert isinstance(reader.database_info, DataBaseInfo)

    def test_plot(self):
        waves = {
            "p_onsets": [100, 1100],
            "p_offsets": [110, 1110],
            "q_onsets": [115, 1115],
            "s_offsets": [130, 1130],
            "t_onsets": [150, 1150],
            "t_offsets": [190, 1190],
        }
        reader.plot(0, leads="II", ticks_granularity=2, waves=waves)
        waves = {
            "p_peaks": [105, 1105],
            "q_peaks": [120, 1120],
            "s_peaks": [125, 1125],
            "t_peaks": [170, 1170],
        }
        reader.plot(0, leads=["II", 7], ticks_granularity=1, waves=waves)
        waves = {
            "p_peaks": [105, 1105],
            "r_peaks": [122, 1122],
            "t_peaks": [170, 1170],
        }
        data = reader.load_data(0)
        reader.plot(0, data=data, ticks_granularity=0, waves=waves, same_range=True)

    def test_compute_metrics(self):
        classes = dx_mapping_scored.Abbreviation.tolist()
        n_records, n_classes = 32, len(classes)
        truth = DEFAULTS.RNG_randint(0, 1, size=(n_records, n_classes))
        probs = DEFAULTS.RNG.uniform(size=(n_records, n_classes))
        thresholds = DEFAULTS.RNG.uniform(size=(n_classes,))
        binary_pred = (probs > thresholds).astype(int)

        metrics = compute_metrics(
            classes=classes,
            truth=truth,
            binary_pred=binary_pred,
            scalar_pred=probs,
        )
        assert isinstance(metrics, tuple)
        assert all([isinstance(m, float) for m in metrics]), [(m, type(m)) for m in metrics]

        metrics = compute_metrics_detailed(
            classes=classes,
            truth=truth,
            binary_pred=binary_pred,
            scalar_pred=probs,
        )
        assert isinstance(metrics, tuple)
        assert all([isinstance(m, (float, np.ndarray)) for m in metrics]), [(m, type(m)) for m in metrics]

    def test_aux_data(self):
        mat = load_weights(return_fmt="np")
        assert isinstance(mat, np.ndarray)
        mat = load_weights(return_fmt="pd")
        assert isinstance(mat, pd.DataFrame)
        with pytest.raises(ValueError, match="format of `torch` is not supported"):
            load_weights(return_fmt="torch")

        assert get_class("713426002") == get_class(713426002)

        class_count_a = get_class_count(tranches="ABCDEF", exclude_classes=["713426002"], fmt="a")
        assert isinstance(class_count_a, dict) and len(class_count_a) > 0
        class_count_f = get_class_count(tranches="ABCDEF", exclude_classes=["713426002"], fmt="f")
        assert isinstance(class_count_f, dict) and len(class_count_f) > 0
        class_count_s = get_class_count(tranches="ABCDEF", exclude_classes=["713426002"], fmt="s")
        assert isinstance(class_count_s, dict) and len(class_count_s) > 0

        class_weight_a = get_class_weight(tranches="ABCDEF", exclude_classes=["713426002"], fmt="a")
        assert isinstance(class_weight_a, dict) and class_weight_a.keys() == class_count_a.keys()
        class_weight_f = get_class_weight(tranches="ABCDEF", exclude_classes=["713426002"], fmt="f")
        assert isinstance(class_weight_f, dict) and class_weight_f.keys() == class_count_f.keys()
        class_weight_s = get_class_weight(tranches="ABCDEF", exclude_classes=["713426002"], fmt="s")
        assert isinstance(class_weight_s, dict) and class_weight_s.keys() == class_count_s.keys()

        with pytest.raises(ValueError, match="`dx_cooccurrence_all` is not found, pre-compute it first"):
            cooccurrence = get_cooccurrence(713426002, "270492004")
        reader._compute_cooccurrence()
        cooccurrence = get_cooccurrence(713426002, "270492004")
        assert isinstance(cooccurrence, int) and cooccurrence >= 0
        with pytest.raises(ValueError, match="class `164951009` not among the scored classes"):
            get_cooccurrence("713426002", "164951009", ensure_scored=True)


config = deepcopy(CINC2021TrainCfg)
config.db_dir = _CWD

with pytest.warns(RuntimeWarning, match="`db_dir` is specified in both config and reader_kwargs"):
    ds = CINC2021Dataset(config, training=False, lazy=False, db_dir=_CWD)


class TestCINC2021Dataset:
    def test_len(self):
        assert len(ds) == len(ds.records) > 0

    def test_getitem(self):
        for i in range(len(ds)):
            data, target = ds[i]
            assert data.ndim == 2 and data.shape == (
                len(config.leads),
                config.input_len,
            )
            assert target.ndim == 1 and target.shape == (len(config.classes),)

        # test slice indexing
        data, target = ds[:2]
        assert data.shape == (2, len(config.leads), config.input_len)
        assert target.shape == (2, len(config.classes))

    def test_load_one_record(self):
        for rec in ds.records:
            data, target = ds._load_one_record(rec)
            assert data.shape == (1, len(config.leads), config.input_len)
            assert target.shape == (1, len(config.classes))

    def test_properties(self):
        assert ds.signals.shape == (
            len(ds.records),
            len(config.leads),
            config.input_len,
        )
        assert ds.labels.shape == (len(ds.records), len(config.classes))
        assert str(ds) == repr(ds)

    def test_to(self):
        new_ds = CINC2021Dataset(config, training=False, lazy=False)
        new_ds.to(six_leads)
        assert new_ds.signals.shape == (len(new_ds.records), 6, config.input_len)
        assert new_ds.labels.shape == (len(new_ds.records), len(config.classes))
        new_ds.to(two_leads)
        assert new_ds.signals.shape == (len(new_ds.records), 2, config.input_len)
        assert new_ds.labels.shape == (len(new_ds.records), len(config.classes))

        with pytest.raises(
            AssertionError,
            match="One is not able to change to a set of leads which is not a subset of the current leads",
        ):
            new_ds.to(twelve_leads)

        del new_ds

    def test_empty(self):
        new_ds = CINC2021Dataset(config, training=False, lazy=False)
        new_ds.empty()
        assert new_ds.signals.shape == (0, len(config.leads), config.input_len)
        assert new_ds.labels.shape == (0, len(config.classes))
        new_ds.empty(four_leads)
        assert new_ds.signals.shape == (0, 4, config.input_len)
        assert new_ds.labels.shape == (0, len(config.classes))
        del new_ds

    def test_from_extern(self):
        new_config = deepcopy(config)
        new_config.leads = deepcopy(three_leads)
        new_ds = CINC2021Dataset.from_extern(ds, new_config)
        assert new_ds.signals.shape == (len(ds.records), 3, config.input_len)
        assert new_ds.labels.shape == (len(ds.records), len(config.classes))
        del new_ds, new_config

    def test_reload_from_extern(self):
        new_config = deepcopy(config)
        new_config.leads = deepcopy(six_leads)
        new_ds = CINC2021Dataset.from_extern(ds, new_config)
        new_ds.empty()
        assert new_ds.signals.shape == (0, 6, config.input_len)
        assert new_ds.labels.shape == (0, len(config.classes))
        new_ds.reload_from_extern(ds)
        assert new_ds.signals.shape == (len(ds.records), 6, config.input_len)
        assert new_ds.labels.shape == (len(ds.records), len(config.classes))

        with pytest.raises(
            AssertionError,
            match="One is not able to reload from a dataset whose `leads` is not a superset of the current one",
        ):
            ds.reload_from_extern(new_ds)

        del new_ds, new_config

    def test_persistence(self):
        ds.persistence()

    def test_check_nan(self):
        ds._check_nan()

    def test_train_test_split(self):
        ds._train_test_split()

        ns = "_ns" if len(ds.config.special_classes) == 0 else ""
        _test_ratio = 20
        _train_ratio = 100 - _test_ratio
        file_suffix = f"_siglen_{ds.siglen}{ns}.json"
        train_file = ds.reader.db_dir_base / f"{ds.reader.db_name}_train_ratio_{_train_ratio}{file_suffix}"
        test_file = ds.reader.db_dir_base / f"{ds.reader.db_name}_test_ratio_{_test_ratio}{file_suffix}"
        assert train_file.exists() and test_file.exists()

        train_set = json.loads(train_file.read_text())
        test_set = json.loads(test_file.read_text())

        _TRANCHES = list("ABEFG")
        for t in _TRANCHES:
            ds._check_train_test_split_validity(
                train_set[t],
                test_set[t],
                set(ds.config.tranche_classes[t]),
            )
