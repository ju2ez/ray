import logging
from pathlib import Path

import numpy as np
from typing import TYPE_CHECKING, Dict, Optional, List

from ray.tune.logger.logger import LoggerCallback
from ray.tune.result import (
    TRAINING_ITERATION,
    TIME_TOTAL_S,
    TIMESTEPS_TOTAL,
)
from ray.tune.utils import flatten_dict
from ray.util.annotations import PublicAPI

if TYPE_CHECKING:
    from ray.tune.experiment.trial import Trial  # noqa: F401
try:
    from aim.ext.resource import DEFAULT_SYSTEM_TRACKING_INT
    from aim.sdk import Run
except ImportError:
    DEFAULT_SYSTEM_TRACKING_INT = None
    Run = None

logger = logging.getLogger(__name__)

VALID_SUMMARY_TYPES = [int, float, np.float32, np.float64, np.int32, np.int64]


@PublicAPI
class AimCallback(LoggerCallback):
    """Aim Logger, logs metrics in Aim format.

    Aim is an open-source, self-hosted ML experiment tracking tool.
    It's good at tracking lots (1000s) of training runs, and it allows you to compare them with a
    performant and well-designed UI.

    Source: https://github.com/aimhubio/aim


    Arguments:
        repo (:obj:`str`, optional): Aim repository path or Repo object to which Run object is bound.
            If skipped, default Repo is used.
        experiment (:obj:`str`, optional): Sets Run's `experiment` property. 'default' if not specified.
            Can be used later to query runs/sequences.
        metrics (:obj:`List[str]`, optional): Specific metrics to track.
            If no metric is specified, log everything that is reported.
        as_multirun (:obj:`bool`, optional): Enable/Disable creating new runs for each trial.
        system_tracking_interval (:obj:`int`, optional): Sets the tracking interval in seconds for system usage
            metrics (CPU, Memory, etc.). Set to `None` to disable system metrics tracking.
        log_system_params (:obj:`bool`, optional): Enable/Disable logging of system params such as installed packages,
            git info, environment variables, etc.

    For more arguments please see the Aim documentation: https://aimstack.readthedocs.io/en/latest/refs/sdk.html
    """

    VALID_HPARAMS = (str, bool, int, float, list, type(None))
    VALID_NP_HPARAMS = (np.bool8, np.float32, np.float64, np.int32, np.int64)

    def __init__(
        self,
        repo: Optional[str] = None,
        experiment: Optional[str] = None,
        metrics: Optional[List[str]] = None,
        as_multirun: Optional[bool] = False,
        **aim_run_kwargs
    ):
        """
        See help(AimCallback) for more information about parameters.
        """
        assert Run is not None, (
            "aim must be installed!. You can install aim with"
            " the command: `pip install aim`."
        )
        self._repo_path = repo
        self._experiment_name = experiment
        assert bool(metrics) or metrics is None
        self._metrics = metrics
        self._as_multirun = as_multirun
        self._run_cls = Run
        self._aim_run_kwargs = aim_run_kwargs
        self._trial_run: Dict["Trial", Run] = {}

    def _create_run(self, trial: "Trial") -> Run:
        """
        Returns:
            run (:obj:`aim.sdk.Run`): The created aim run for a specific trial.
        """
        experiment_dir = str(Path(trial.logdir).parent)
        run = self._run_cls(
            repo=self._repo_path or experiment_dir,
            experiment=self._experiment_name or trial.experiment_dir_name,
            **self._aim_run_kwargs
        )
        if self._as_multirun:
            run["trial_id"] = trial.trial_id
        return run

    def log_trial_start(self, trial: "Trial"):
        if self._as_multirun:
            if trial in self._trial_run:
                self._trial_run[trial].close()
        elif self._trial_run:
            return

        trial.init_logdir()
        self._trial_run[trial] = self._create_run(trial)

        # log hyperparameters
        if trial and trial.evaluated_params:
            flat_result = flatten_dict(trial.evaluated_params, delimiter="/")
            scrubbed_result = {
                k: value
                for k, value in flat_result.items()
                if isinstance(value, tuple(VALID_SUMMARY_TYPES))
            }
            self._log_hparams(trial, scrubbed_result)

    def log_trial_result(self, iteration: int, trial: "Trial", result: Dict):
        # create local copy to avoid problems
        tmp_result = result.copy()

        step = result.get(TIMESTEPS_TOTAL) or result[TRAINING_ITERATION]

        for k in ["config", "pid", "timestamp", TIME_TOTAL_S, TRAINING_ITERATION]:
            if k in tmp_result:
                del tmp_result[k]  # not useful to log these

        context = tmp_result.pop("context", None)
        epoch = tmp_result.pop("epoch", None)

        trial_run = self._get_trial_run(trial)
        if not self._as_multirun:
            context["trial"] = trial.trial_id

        path = ["ray", "tune"]

        if self._metrics:
            flat_result = flatten_dict(tmp_result, delimiter="/")
            valid_result = {}
            for metric in self._metrics:
                full_attr = "/".join(path + [metric])
                value = flat_result[metric]
                if isinstance(value, tuple(VALID_SUMMARY_TYPES)) and not np.isnan(
                    value
                ):
                    valid_result[metric] = value
                    try:
                        trial_run.track(
                            value=tmp_result[metric],
                            epoch=epoch,
                            name=full_attr,
                            step=step,
                            context=context,
                        )
                    except KeyError:
                        logger.warning(
                            f"The metric {metric} is specified but not reported."
                        )
                elif (isinstance(value, list) and len(value) > 0) or (
                    isinstance(value, np.ndarray) and value.size > 0
                ):
                    valid_result[metric] = value
        else:
            # if no metric is specified log everything that is reported
            flat_result = flatten_dict(tmp_result, delimiter="/")
            valid_result = {}

            for attr, value in flat_result.items():
                full_attr = "/".join(path + [attr])
                if isinstance(value, tuple(VALID_SUMMARY_TYPES)) and not np.isnan(
                    value
                ):
                    valid_result[attr] = value
                    trial_run.track(
                        value=value, name=full_attr, epoch=epoch, step=step, context=context
                    )
                elif (isinstance(value, list) and len(value) > 0) or (
                    isinstance(value, np.ndarray) and value.size > 0
                ):
                    valid_result[attr] = value

    def log_trial_end(self, trial: "Trial", failed: bool = False):
        # cleanup in the end
        trial_run = self._get_trial_run(trial)
        trial_run.close()
        del trial_run

    def _log_hparams(self, trial: "Trial", params: Dict):
        flat_params = flatten_dict(params)

        scrubbed_params = {
            k: v for k, v in flat_params.items() if isinstance(v, self.VALID_HPARAMS)
        }

        np_params = {
            k: v.tolist()
            for k, v in flat_params.items()
            if isinstance(v, self.VALID_NP_HPARAMS)
        }

        scrubbed_params.update(np_params)
        removed = {
            k: v
            for k, v in flat_params.items()
            if not isinstance(v, self.VALID_HPARAMS + self.VALID_NP_HPARAMS)
        }
        if removed:
            logger.info(
                "Removed the following hyperparameter values when "
                "logging to aim: %s",
                str(removed),
            )

        self._trial_run[trial]["hparams"] = scrubbed_params

    def _get_trial_run(self, trial):
        if not self._as_multirun:
            return list(self._trial_run.values())[0]
        return self._trial_run[trial]
