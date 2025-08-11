from __future__ import annotations

import argparse
import itertools
import logging
import pathlib
import time
from datetime import datetime
from typing import Dict, Any, List

import pandas as pd
from omegaconf import OmegaConf
from argparse import Namespace 
import os

os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
os.environ["CUDA_VISIBLE_DEVICES"] = "3"
import torch
torch.use_deterministic_algorithms(True)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False


import torch.cuda

from src.dataloader import Mimic, Boiler, MITECG, PAM, SimulatedSwitch, SimulatedState, SimulatedSpike, \
    WinITDataset, SimulatedData, SimulatedL2X, SeqCombMV
from src.explainer.explainers import BaseExplainer, DeepLiftExplainer, IGExplainer, \
    GradientShapExplainer
from src.explainer.masker import Masker
from src.explanationrunner import ExplanationRunner
from src.utils.basic_utils import append_df_to_csv
from src.datagen.spikes_data_new import SpikeTrainDataset 


class Params:
    def __init__(self, argdict: Dict[str, Any]):
        self.argdict = argdict

        self._all_explainer_dict: Dict[str, List[Dict[str, Any]]] | None = None
        self._generators_to_train: Dict[str, List[Dict[str, Any]]] | None = None

        self._outpath: pathlib.Path | None = None
        self._ckptpath: pathlib.Path | None = None
        self._plotpath: pathlib.Path | None = None
        self._model_args: Dict[str, Any] | None = None
        self._model_train_args: Dict[str, Any] | None = None

        self._datasets = self._resolve_datasets()
        self._resolve_model_args()
        self._resolve_explainers()
        self._init_logging()

    @property
    def datasets(self) -> WinITDataset:
        return self._datasets

    @property
    def model_args(self) -> Dict[str, Any]:
        return {} if self._model_args is None else self._model_args

    @property
    def model_train_args(self) -> Dict[str, Any]:
        return {} if self._model_train_args is None else self._model_train_args

    @property
    def all_explainer_dict(self) -> Dict[str, List[Dict[str, Any]]]:
        return {} if self._all_explainer_dict is None else self._all_explainer_dict

    @property
    def generators_to_train(self) -> Dict[str, List[Dict, str, Any]]:
        return {} if self._generators_to_train is None else self._generators_to_train

    @property
    def outpath(self) -> pathlib.Path | None:
        return None if self._outpath is None else self._outpath

    @property
    def ckptpath(self) -> pathlib.Path | None:
        return None if self._ckptpath is None else self._ckptpath

    @property
    def plotpath(self) -> pathlib.Path | None:
        return None if self._plotpath is None else self._plotpath

    def _resolve_datasets(self) -> WinITDataset:
        data = self.argdict["data"]
        testbs = self.argdict["testbs"]
        batch_size = self.argdict["batchsize"]
        data_path = self.argdict["datapath"]
        data_seed = self.argdict["dataseed"]
        cv_to_use = self.argdict["cv"]
        nondeterministic = self.argdict["nondeterministic"]
        kwargs = {"batch_size": batch_size, "seed": data_seed, "cv_to_use": cv_to_use,
                  "deterministic": not nondeterministic}


        if data_path is not None:
            kwargs["data_path"] = data_path

        if data == "spike":
            kwargs["testbs"] = 300 if testbs == -1 else testbs
            delay = self.argdict["delay"]
            return SimulatedSpike(delay=delay, **kwargs)

        if data == "mimic":
            kwargs["testbs"] = 1000 if testbs == -1 else testbs
            return Mimic(**kwargs)
        
        if (data == "boiler"):
            kwargs["testbs"] = 1000 if testbs == -1 else testbs
            return Boiler( **kwargs)
        
        if (data == "mitecg") or (data == "mitecg1"):
            kwargs["testbs"] = 1000 if testbs == -1 else testbs
            return MITECG( **kwargs)
        
        if (data == "pam"):
            kwargs["testbs"] = 1000 if testbs == -1 else testbs
            return PAM( **kwargs)

        if data == "switch":
            kwargs["testbs"] = 300 if testbs == -1 else testbs
            return SimulatedSwitch(**kwargs)

        if data == "state":
            kwargs["testbs"] = 300 if testbs == -1 else testbs
            return SimulatedState(**kwargs)
        
        if data == "simulated_data_l2x":
            kwargs["testbs"] = 300 if testbs == -1 else testbs
            return SimulatedL2X(**kwargs)
        
        if data == "seqcombmv":
            kwargs["testbs"] = 300 if testbs == -1 else testbs
            return SeqCombMV(**kwargs)

        raise ValueError(f"Unknown data {data}")

    def _resolve_explainers(self) -> None:
        explainers = self.argdict["explainer"]
        nsamples = self.argdict["samples"]
        all_explainer_dict = {}
        generator_dict = {}
        for explainer in explainers:
            if explainer == "dynamask":
                explainer_dict = self._resolve_dynamask_explainer_dict()
                all_explainer_dict[explainer] = [explainer_dict]
            elif explainer in ["winit", "biwinit", "jimex"]:
                windows = self.argdict["window"]
                winit_metrics = self.argdict["winitmetric"]
                
                
                winit_explainer_dict_list = []
                generator_dict_list = []
                for window in windows:
                    explainer_dict_window = {
                        "window_size": window,
                        "joint": self.argdict["joint"],
                        "conditional": self.argdict["conditional"],
                        "usedatadist": self.argdict['usedatadist'],
                        "random_state": self.argdict["explainerseed"],
                    }

                    if explainer == "biwinit":
                        height = self.argdict["height"]
                        mask_strategy = self.argdict["mask_strategy"]
                        explainer_dict_window["height"] = height
                        explainer_dict_window["mask_strategy"] = mask_strategy
                    if nsamples != -1:
                        explainer_dict_window["n_samples"] = nsamples
                    for winit_metric in winit_metrics:
                        explainer_dict = explainer_dict_window.copy()
                        explainer_dict["metric"] = winit_metric
                        winit_explainer_dict_list.append(explainer_dict)

                    generator_dict_list.append(explainer_dict_window)
                all_explainer_dict[explainer] = winit_explainer_dict_list
                generator_dict[explainer] = generator_dict_list
            else:
                explainer_dict = {}
                if explainer in ["fit", "fo", "afo"] and nsamples != -1:
                    explainer_dict["n_samples"] = nsamples
                if explainer == "fit":
                    generator_dict["fit"] = [explainer_dict]
                all_explainer_dict[explainer] = [explainer_dict]
        self._all_explainer_dict = all_explainer_dict
        self._generators_to_train = generator_dict

    def _resolve_dynamask_explainer_dict(self) -> Dict[str, Any]:
        data = self.argdict["data"]
        area = self.argdict["area"]
        loss = self.argdict["loss"]
        timereg = self.argdict["timereg"]
        sizereg = self.argdict["sizereg"]
        deletion_mode = self.argdict["deletion"]
        blur_type = self.argdict["blurtype"]
        use_last_timestep_only = self.argdict["lastonly"]
        explainer_dict = {"num_epoch": self.argdict["epoch"]}
        if loss is not None:
            explainer_dict["loss"] = loss
        if area is not None:
            explainer_dict["area_list"] = area
        elif data == "mimic":
            explainer_dict["area_list"] = [0.05]
        if timereg is not None:
            explainer_dict["time_reg_factor"] = timereg
        elif data == "mimic":
            explainer_dict["time_reg_factor"] = 0
        if sizereg is not None:
            explainer_dict["size_reg_factor_dilation"] = sizereg
        elif data == "mimic":
            explainer_dict["size_reg_factor_dilation"] = 10000
        if deletion_mode is not None:
            explainer_dict["deletion_mode"] = deletion_mode
        elif data == "mimic":
            explainer_dict["deletion_mode"] = True
        if blur_type is not None:
            explainer_dict["blur_type"] = blur_type
        elif data == "mimic":
            explainer_dict["blur_type"] = "fadema"
        if use_last_timestep_only is not None:
            explainer_dict["use_last_timestep_only"] = use_last_timestep_only == "True"
        elif data == "mimic":
            explainer_dict["use_last_timestep_only"] = False
        return explainer_dict

    def _resolve_model_args(self) -> None:
        hidden_size = self.argdict['hiddensize']
        dropout = self.argdict['dropout']
        num_layers = self.argdict['numlayers']
        model_type = self.argdict['modeltype'].upper()
        lr = self.argdict['lr']
        self._model_args = {"hidden_size": hidden_size,
                            "dropout": dropout,
                            "num_layers": num_layers,
                            "model_type": model_type,
                            }

        if lr is None:
            lr = 1e-4 if isinstance(self._datasets, Mimic) else 1e-3

        if isinstance(self._datasets, Mimic):
            if model_type == "GRU":
                num_epochs = 100
            elif model_type == "CONV":
                num_epochs = 10
            elif model_type == "LSTM":
                num_epochs = 30
        else:
            num_epochs = self.argdict['epochs_classifier']
        self._model_train_args = {"num_epochs": num_epochs, "lr": lr}

        base_out_path = pathlib.Path(self.argdict["outpath"])
        base_ckpt_path = pathlib.Path(self.argdict["ckptpath"])
        base_plot_path = pathlib.Path(self.argdict["plotpath"])
        self._outpath = self._resolve_path(base_out_path, model_type, num_layers)
        self._ckptpath = self._resolve_path(base_ckpt_path, model_type, num_layers)
        self._plotpath = self._resolve_path(base_plot_path, model_type, num_layers)

    def _init_logging(self) -> logging.Logger:
        format = '%(asctime)s %(levelname)8s %(name)25s: %(message)s'
        log_formatter = logging.Formatter(format)

        if self.argdict["logfile"] is None:
            time_str = datetime.now().strftime("%Y%m%d-%H%M")
            log_file_name = f"log_{time_str}.log"
        else:
            log_file_name = self.argdict["logfile"]

        log_path = pathlib.Path(self.argdict["logpath"])
        log_path.mkdir(parents=True, exist_ok=True)
        log_file_name = log_path / log_file_name
        logging.basicConfig(format=format,
                            level=logging.getLevelName(self.argdict['loglevel'].upper()))

        root_logger = logging.getLogger()
        file_handler = logging.FileHandler(str(log_file_name))
        file_handler.setFormatter(log_formatter)
        root_logger.addHandler(file_handler)
        return root_logger

    def _resolve_path(self, base_path: pathlib.Path, model_type: str, num_layers: int):
        if model_type == "GRU":
            return base_path / f"gru{num_layers}layer"
        elif model_type == "LSTM":
            return base_path / "lstm"
        elif model_type == "CONV":
            return base_path / "conv"
        else:
            raise Exception("Unknown model type ({})".format(model_type))

    def get_maskers(self, explainer: BaseExplainer) -> List[Masker]:
        maskers = []
        seed = self.argdict["maskseed"]
        absolutize = isinstance(explainer,
                                (DeepLiftExplainer, IGExplainer, GradientShapExplainer))
        for drop, aggregate_method in itertools.product(self.argdict["drop"],
                                                        self.argdict["aggregate"]):
            if drop == "bal":
                mask_methods = ["std"]
                top = self.argdict["top"]
                balanced = True
            elif drop == "local":
                mask_methods = self.argdict["mask"]
                top = self.argdict["top"]
                balanced = False
            else:
                mask_methods = self.argdict["mask"]
                top = self.argdict["toppc"]
                balanced = False

            for mask_method in mask_methods:
                maskers.append(
                    Masker(mask_method, top, balanced, seed, absolutize, aggregate_method))
        return maskers


if __name__ == '__main__':

    # 1) parse only the --config argument (leave all other CLI args alone)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", "-c", type=str, default="config/main.yaml", help="Path to the OmegaConf YAML file",)
    args, remaining_argv = parser.parse_known_args()

    # 2) load the chosen YAML
    cfg = OmegaConf.load(args.config)



    argdict = OmegaConf.to_container(cfg, resolve=True)

    # argdict = vars(parser.parse_args())
    data = argdict['data']
    explainers = argdict['explainer']
    skip_explain = argdict['skipexplain']
    eval_explain = argdict['eval']
    train_models = argdict['train']
    train_gen = argdict['traingen']
    result_file = argdict["resultfile"]
    epoch_gen = argdict["epoch_gen"]
    train_ratio = argdict.get("train_ratio") or 0.8
    device = "cuda" if torch.cuda.is_available() else "cpu"
    # device = "cpu"

    # parse the arg
    params = Params(argdict)
    dataset = params.datasets
    model_args = params.model_args
    model_train_args = params.model_train_args
    all_explainer_dict = params.all_explainer_dict
    out_path = params.outpath
    ckpt_path = params.ckptpath
    plot_path = params.plotpath
    start_time = time.time()
    log = logging.getLogger("Base")
    for k, v in argdict.items():
        log.info(f"{k:15}: {v}")
    first = True
    save_failed = False

    all_df = []
    try:
        # load data and train model
        dataset.load_data(train_ratio=train_ratio)
        runner = ExplanationRunner(dataset, device, out_path, ckpt_path, plot_path)
        # import torch
        # torch.cuda.empty_cache()

        runner.init_model(**model_args)
        use_all_times = not isinstance(dataset, (SeqCombMV, Mimic, Boiler, MITECG, PAM))
        if train_models:
            runner.train_model(**model_train_args, use_all_times=use_all_times)
        else:
            runner.load_model(use_all_times)
        log.info("Load data and train/load model done.")

        # train generators
        if train_gen:
            print('training gen for {} epochs'.format(epoch_gen))
            generators_to_train = params.generators_to_train
            for explainer_name, explainer_dict_list in generators_to_train.items():
                for explainer_dict in explainer_dict_list:
                    runner.get_explainers(explainer_name, explainer_dict=explainer_dict)
                    log.info(
                        f"Training Generator...Data={dataset.get_name()}, Explainer={explainer_name}")
                    runner.train_generators(num_epochs=epoch_gen)
            log.info("Training Generator Done.")

        for explainer_name, explainer_dict_list in all_explainer_dict.items():
            for explainer_dict in explainer_dict_list:
                # generate feature importance
                runner.clean_up(clean_importance=True, clean_explainer=True, clean_model=False)
                runner.get_explainers(explainer_name, explainer_dict=explainer_dict)
                runner.set_model_for_explainer(set_eval=explainer_name != "fit")

                if not skip_explain:
                    log.info(f"Running Explanations..."
                             f"Data={dataset.get_name()}, Explainer={explainer_name}, Dict={explainer_dict}")

                    # runner.load_generators()
                    runner.run_attributes()
                    runner.save_importance()
                    importances = runner.importances
                    log.info("Explanations done.")

                # evaluate importances
                if eval_explain:
                    log.info("Evaluating importance...")
                    log.info(f"Data={dataset.get_name()}, Explainer={explainer_name}")
                    if runner.importances is None:
                        runner.load_importance()
                    if isinstance(dataset, SimulatedData):
                        df = runner.evaluate_simulated_importance(argdict["aggregate"])
                    else:
                        maskers = params.get_maskers(next(iter(runner.explainers.values())))
                        df = runner.evaluate_performance_drop(maskers, use_last_time_only=True)
                    log.info("Evaluating importance done.")

                    # Prepare the result dataframe to be saved.
                    df = df.reset_index()
                    original_columns = df.columns
                    now = datetime.now()
                    timestr = now.strftime("%Y%m%d-%H%M")
                    df["date"] = timestr
                    df["dataset"] = dataset.get_name()
                    df["explainer"] = next(iter(runner.explainers.values())).get_name()
                    df = df[["dataset", "explainer", "date"] + list(original_columns)]
                    all_df.append(df)
                    result_path = out_path / dataset.get_name()
                    result_path.mkdir(parents=True, exist_ok=True)
                    error_code = append_df_to_csv(df, result_path / result_file)
                    if first:
                        first = False
                    else:
                        if error_code != 0:
                            save_failed = False
        log.info(f"All done! Time elapsed: {time.time() - start_time}")
    except (Exception, KeyboardInterrupt) as e:
        if save_failed and len(all_df) > 0:
            result_file_bak = result_file + ".bak"
            pd.concat(all_df, axis=0, ignore_index=True).to_csv(str(out_path / result_file_bak),
                                                                index=False)
            log.info(f"Error! Emergency saved to {out_path / result_file_bak}")
        log.info(f"Time elapsed: {time.time() - start_time}")
        log.exception(e)
        raise e
