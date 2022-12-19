"""
"""

import itertools
from copy import deepcopy
from pathlib import Path

import pytest
import torch
from tqdm.auto import tqdm

from torch_ecg.models.ecg_seq_lab_net import ECG_SEQ_LAB_NET, ECG_SEQ_LAB_NET_v1
from torch_ecg.model_configs.ecg_seq_lab_net import ECG_SEQ_LAB_NET_CONFIG
from torch_ecg.utils.utils_nn import adjust_cnn_filter_lengths


_TMP_DIR = Path(__file__).parents[1] / "tmp"
_TMP_DIR.mkdir(exist_ok=True)


def test_ecg_seq_lab_net():
    inp = torch.randn(2, 12, 5000)
    fs = 400
    classes = ["N"]

    grid = itertools.product(
        ["multi_scopic", "multi_scopic_leadwise"],  # cnn
        ["none", "lstm"],  # rnn
        ["none", "se", "gc", "nl"],  # attn
        [True, False],  # recover_length
    )
    total = 2 * 2 * 4 * 2

    for cnn, rnn, attn, recover_length in tqdm(grid, total=total):
        config = adjust_cnn_filter_lengths(ECG_SEQ_LAB_NET_CONFIG, fs)
        config.cnn.name = cnn
        config.rnn.name = rnn
        config.attn.name = attn
        config.recover_length = recover_length

        model = ECG_SEQ_LAB_NET(classes=classes, n_leads=12, config=config)
        out = model(inp)
        assert out.shape == model.compute_output_shape(
            seq_len=inp.shape[-1], batch_size=inp.shape[0]
        )
        if recover_length:
            assert out.shape[1] == inp.shape[-1]

    with pytest.warns(
        RuntimeWarning, match="No config is provided, using default config"
    ):
        model = ECG_SEQ_LAB_NET(classes=classes, n_leads=12)

    doi = model.doi
    assert isinstance(doi, list)
    assert all([isinstance(d, str) for d in doi])

    with pytest.raises(
        NotImplementedError, match="implement a task specific inference method"
    ):
        model.inference(inp)


def test_from_v1():
    config = deepcopy(ECG_SEQ_LAB_NET_CONFIG)
    n_leads = 12
    classes = ["N"]
    model_v1 = ECG_SEQ_LAB_NET_v1(classes=classes, n_leads=n_leads, config=config)
    model_v1.save(
        _TMP_DIR / "ecg_seq_lab_net_v1.pth", {"classes": classes, "n_leads": n_leads}
    )
    model = ECG_SEQ_LAB_NET.from_v1(_TMP_DIR / "ecg_seq_lab_net_v1.pth")
    (_TMP_DIR / "ecg_seq_lab_net_v1.pth").unlink()
    del model_v1, model
