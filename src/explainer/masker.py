from __future__ import annotations

from typing import Dict

import numpy as np

from src.utils.basic_utils import aggregate_scores


class Masker:
    """
    A class used to compute performance drop using different masking methods.
    """

    def __init__(
        self,
        mask_method: str,
        top: int | float,
        balanced: bool,
        seed: int,
        absolutize: bool,
        aggregate_method: str = "mean",
    ):
        """
        Constructor

        Args:
            mask_method:
                The masking method. Must be "end", "std" or "end_fit". Note that "end" will
                mask the all subsequent observations if the observation is deemed important. "std"
                will mask all subsequent observations until the value changed by 1 STD of the
                feature distribution. "end_fit" is like "end", but with the code of the original
                FIT repository and started masking when t >= 10.
            top:
               If int, it will mask the top `top` observations of each time series. If float, it
               will mask the top `top*100` percent of the observations for all time series.
            balanced:
               If True, the number of masked will be balanced std. It is used for the STD-BAL
               masking method in the paper.
            seed:
               The random seed.
            absolutize:
               Indicate whether we should absolutize the feature importance for determining the
               top features.
            aggregate_method:
               For features that contain a window size, i.e. WinIT, this describes the aggregation
               method. It can be "absmax", "max", "mean".
        """
        if mask_method not in ["end", "std", "end_fit"]:
            raise NotImplementedError(f"Mask method {mask_method} unrecognized")
        self.mask_method = mask_method
        self.top = top
        self.balanced = balanced
        self.seed = seed
        self.local = isinstance(top, int)
        min_time_dict = {"std": 1, "end": 1, "end_fit": 10}
        self.min_time = min_time_dict[self.mask_method]
        self.importance_threshold = -1000
        self.absolutize = absolutize
        self.aggregate_method = aggregate_method
        assert not balanced or self.local and mask_method in ["std", "end"]

        self.start_masked_count = None
        self.all_masked_count = None
        self.feature_masked = None

    def mask(
        self, x_test: np.ndarray, importance_scores: Dict[int, np.ndarray]
    ) -> Dict[int, np.ndarray]:
        """
        Perform masking.

        Args:
            x_test:
                The original input of shape (num_samples, num_features, num_times)
            importance_scores:
                The importance score dictionary from CV to numpy arrays.

        Returns:
            A dictionary from CV to numpy arrays of shape (num_samples, num_features, num_times)
            that has x_test masked.

        """
        new_xs = {}
        start_masked_count = {}
        all_masked_count = {}
        feature_masked = {}
        num_samples, num_features, num_times = x_test.shape
        for cv, importance_score in importance_scores.items():
            importance_score = aggregate_scores(importance_score, self.aggregate_method)
            if self.absolutize:
                importance_score = np.abs(importance_score)
            coordinate_list = self._generate_arg_sort(
                importance_score, randomize_ties=True
            )  # (num_coordinates, 3)

            new_x = x_test.copy()
            masked = np.zeros_like(new_x, dtype=bool)
            start_masked = np.zeros_like(new_x, dtype=bool)
            if self.mask_method in ["std", "end"]:
                if not self.local:
                    num_feature_time_drop = int(len(coordinate_list) * self.top)
                    coordinate_list = coordinate_list[:num_feature_time_drop]
                    for coordinate in coordinate_list:
                        sample_id, feature_id, time_id = coordinate
                        if (
                            importance_score[sample_id, feature_id, time_id]
                            <= self.importance_threshold
                        ):
                            # not important enough. Not masking.
                            break
                        if masked[sample_id, feature_id, time_id]:
                            # already masked.
                            continue
                        end_time, new_x[sample_id, feature_id, :] = self._carry_forward(
                            time_id, new_x[sample_id, feature_id, :], self.mask_method
                        )
                        masked[sample_id, feature_id, time_id:end_time] = True
                        start_masked[sample_id, feature_id, time_id] = True
                else:
                    num_feature_time = new_x.shape[1] * (new_x.shape[2] - self.min_time)
                    for sample_id in range(new_x.shape[0]):
                        start = sample_id * num_feature_time
                        num_masked_total = 0
                        num_masked = 0
                        for cur in range(num_feature_time):
                            sample_index, feature_id, time_id = coordinate_list[start + cur]
                            # sanity check
                            if sample_index != sample_id:
                                raise RuntimeError("Failed sanity check!")

                            balance_condition = self.balanced and num_masked_total >= self.top
                            num_drop_condition = num_masked >= self.top
                            threshold_condition = (
                                importance_score[sample_id, feature_id, time_id]
                                <= self.importance_threshold
                            )
                            if threshold_condition or balance_condition or num_drop_condition:
                                # stop masking because any of the condition occurs.
                                break
                            if masked[sample_index, feature_id, time_id]:
                                # already masked.
                                continue
                            end_ts, new_x[sample_id, feature_id, :] = self._carry_forward(
                                time_id, new_x[sample_id, feature_id, :], self.mask_method
                            )
                            masked[sample_id, feature_id, time_id:end_ts] = True
                            start_masked[sample_id, feature_id, time_id] = True
                            num_masked_total += end_ts - time_id
                            num_masked += 1
            elif self.mask_method == "end_fit":
                # Version of the experiments from the original fit paper
                # fmt: off
                for i, x in enumerate(new_x):
                    if not self.local:
                        q = np.percentile(importance_score[:, :, self.min_time:],
                                          100 - self.top * 100)
                        min_t_feat = [
                            np.min(np.where(importance_score[i, f, self.min_time:] >= q)[0]) if
                            len(np.where(importance_score[i, f, self.min_time:] >= q)[0]) > 0 else
                            x.shape[-1] - self.min_time - 1 for f in range(num_features)]
                        for f in range(importance_score[i].shape[0]):
                            x[f, min_t_feat[f] + self.min_time:] = x[
                                f, min_t_feat[f] + self.min_time - 1]
                            masked[i, f, min_t_feat[f] + self.min_time:] = True
                            start_masked[i, f, min_t_feat[f] + self.min_time] = True
                    else:
                        for _ in range(self.top):
                            imp = np.unravel_index(importance_score[i, :, self.min_time:].argmax(),
                                                   importance_score[i, :, self.min_time:].shape)
                            importance_score[i, imp[0], imp[1] + self.min_time:] = -1
                            x[imp[0], imp[1] + self.min_time:] = x[
                                imp[0], imp[1] + self.min_time - 1]
                            masked[i, imp[0], imp[1] + self.min_time:] = True
                            start_masked[i, imp[0], imp[1] + self.min_time] = True
                # fmt: on

            start_masked_count[cv] = np.sum(start_masked, axis=0)
            all_masked_count[cv] = np.sum(masked, axis=0)
            feature_masked[cv] = np.sum(np.sum(start_masked, axis=1) > 0, axis=0)

            new_xs[cv] = new_x

        self.start_masked_count = start_masked_count
        self.all_masked_count = all_masked_count
        self.feature_masked = feature_masked

        return new_xs

    def _generate_arg_sort(self, scores, randomize_ties=True):
        """
        Returns a list of coordinates that is the argument of the sorting. In descending order.
        If local is True, the list of coordinates will be sorted within each sample. i.e.,
        the first (num_features * num_times) coordinates would always correspond to the first
        sample, the next (num_features * num_times) coordinates would correspond to the second
        sample, etc.

        Args:
            scores:
               Importance scores of shape (num_samples, num_features, num_times)
            min_time:
               The minimum timesteps to sort.
            local:
               Indicates whether the sorting is local or global. i.e. along all axes, or just the
               feature and time axes.
            randomize_ties:
               If there is a tie in the scores (which happens very often in Dynamask), we randomly
               permute the coordinates across the tie.

        Returns:
            A array of shape (all_coordinate_length, 3), for each row is the coordinate.
        """
        truncated_scores = scores[:, :, self.min_time :]
        if self.local:
            flattened_scores = truncated_scores.reshape(scores.shape[0], -1)
            argsorted_ravel_local = np.argsort(flattened_scores)[:, ::-1]
            if randomize_ties:
                self._shuffle_ties(argsorted_ravel_local, flattened_scores)
            feature_index = argsorted_ravel_local // truncated_scores.shape[2]
            time_index = (argsorted_ravel_local % truncated_scores.shape[2]) + self.min_time
            arange = np.arange(scores.shape[0]).reshape(-1, 1).repeat(feature_index.shape[1], 1)
            coordinate_list = np.stack([arange, feature_index, time_index]).reshape(
                3, -1
            )  # (3, all_coordinate_length)
            return coordinate_list.transpose()
        else:
            flattened_scores = truncated_scores.ravel()
            argsorted_ravel_global = np.argsort(flattened_scores)[::-1]
            if randomize_ties:
                self._shuffle_ties_global(argsorted_ravel_global, flattened_scores)
            coordinate_list = np.stack(
                np.unravel_index(argsorted_ravel_global, truncated_scores.shape)
            )  # (3, all_coordinate_length)
            coordinate_list[2, :] += self.min_time
            return coordinate_list.transpose()

    def _shuffle_ties_global(self, argsorted_ravel_global, flattened_scores):
        sorted_scores = flattened_scores[argsorted_ravel_global]
        repeated = np.r_[False, sorted_scores[1:] == sorted_scores[:-1]]
        indices = np.where(np.diff(repeated))[0]
        if len(indices) % 2 == 1:
            indices = np.r_[indices, len(sorted_scores) - 1]
        indices = indices.reshape(-1, 2)
        indices[:, 1] += 1
        rng = np.random.default_rng(self.seed)
        for repeated_index in indices:
            from_index, to_index = repeated_index
            rng.shuffle(argsorted_ravel_global[from_index:to_index])

    def _shuffle_ties(self, argsorted_ravel_local, flattened_scores):
        sorted_scores = np.take_along_axis(flattened_scores, argsorted_ravel_local, axis=1)
        repeated = np.concatenate(
            [
                np.zeros((len(sorted_scores), 1), dtype=bool),
                sorted_scores[:, 1:] == sorted_scores[:, :-1],
            ],
            axis=1,
        )
        indices = np.where(np.diff(repeated, axis=1))
        previous_sample_id = -1
        left = -1
        rng = np.random.default_rng(self.seed)
        for sample_id, x in zip(*indices):
            if sample_id != previous_sample_id:
                # new sample seen
                if left != -1:
                    # still have right bound left.
                    right = sorted_scores.shape[1]
                    rng.shuffle(argsorted_ravel_local[previous_sample_id, left:right])
                left = x
            else:
                # same sample
                if left != -1:
                    rng.shuffle(argsorted_ravel_local[previous_sample_id, left : x + 1])
                    left = -1
                else:
                    left = x
            previous_sample_id = sample_id
        if left != -1:
            right = sorted_scores.shape[1]
            rng.shuffle(argsorted_ravel_local[previous_sample_id, left:right])

    def _carry_forward(self, timestep, time_series, mask):
        assert len(time_series.shape) == 1
        ts_length = time_series.shape[0]

        assert timestep != 0, "Carry forward is not defined at index 0"

        if mask == "std":
            threshold = np.std(time_series)
            old = time_series[timestep]
            segment = np.abs(time_series[timestep:] - old) > threshold
            over = np.where(segment)[0]
            new_timestep = ts_length if len(over) == 0 else over[0] + timestep
        elif mask == "end":
            new_timestep = ts_length
        else:
            raise NotImplementedError(f"Mask method {mask} not recognized for carry forward")

        time_series[timestep:new_timestep] = time_series[timestep - 1]
        return new_timestep, time_series

    def get_name(self) -> str:
        if self.local:
            if self.balanced:
                return f"bal{int(self.top)}_{self.mask_method}_{self.aggregate_method}"
            return f"top{int(self.top)}_{self.mask_method}_{self.aggregate_method}"
        return f"globaltop{int(self.top * 100)}_{self.mask_method}_{self.aggregate_method}"


class Masker1:
    """
    A class used to compute performance drop using different masking methods.
    """

    def __init__(
        self,
        mask_method: str,
        top: int | float,
        balanced: bool,
        seed: int,
        absolutize: bool,
        aggregate_method: str = "mean",
        substitution: str = "zero",   # <-- NEW: 'zero' or 'mean'
    ):
        """
        Args:
            mask_method: "cells" (NEW), or legacy: "end", "std", "end_fit".
            top: if int -> top-k cells per sample; if float in (0,1] -> top-(k*100)% cells (per sample for 'cells', global for legacy global mode)
            balanced, seed, absolutize, aggregate_method: (unchanged)
            substitution: how to fill masked cells: 'zero' or 'mean' (per-sample, per-feature temporal mean)
        """
        if mask_method not in ["cells", "end", "std", "end_fit"]:
            raise NotImplementedError(f"Mask method {mask_method} unrecognized")
        self.mask_method = mask_method
        self.top = top
        self.balanced = balanced
        self.seed = seed
        self.local = isinstance(top, int)
        self.absolutize = absolutize
        self.aggregate_method = aggregate_method
        self.substitution = substitution.lower()
        if self.substitution not in ("zero", "mean"):
            raise ValueError("substitution must be 'zero' or 'mean'")

        # legacy-only params
        min_time_dict = {"std": 1, "end": 1, "end_fit": 10}
        self.min_time = min_time_dict.get(self.mask_method, 1)
        self.importance_threshold = -1000
        assert not balanced or self.local and self.mask_method in ["std", "end"]

        self.start_masked_count = None
        self.all_masked_count = None
        self.feature_masked = None

    # -------------------------------
    # NEW: small selector for cells mode
    # -------------------------------
    def _select_cells_per_sample(
        self, scores: np.ndarray, k: int | float, direction: str
    ) -> np.ndarray:
        """
        scores: (N, F, T)
        returns a boolean mask 'sel' of shape (N, F, T) with True for selected cells.
        Selection is per-sample: top-k (direction='top') or bottom-k ('bottom')
        """
        N, F, T = scores.shape
        s = scores.copy()
        order_sign = -1 if direction == "top" else 1  # top -> descending, bottom -> ascending
        # flatten per sample
        flat = s.reshape(N, -1) * order_sign
        idx_sorted = np.argsort(flat, axis=1)  # ascending over signed -> desired order
        if isinstance(k, float):
            if not (0 < k <= 1):
                raise ValueError("k as float must be in (0,1]")
            kk = np.maximum(1, (k * (F * T)).astype(int) if isinstance(k, np.ndarray) else int(round(k * F * T)))
        else:
            kk = int(k)
        kk = max(1, min(kk, F * T))
        sel = np.zeros_like(flat, dtype=bool)
        # take first kk indices (since after sign they’re desired extreme)
        take = idx_sorted[:, :kk]
        row_idx = np.arange(N)[:, None]
        sel[row_idx, take] = True
        return sel.reshape(N, F, T)

    # -------------------------------
    # UPDATED: masking core
    # -------------------------------
    def mask(
        self,
        x_test: np.ndarray,
        importance_scores: Dict[int, np.ndarray],
        k: int | float | None = None,
        direction: str = "top",     # 'top' or 'bottom'
        mode: str = "remove",       # 'remove' or 'keep'
    ) -> Dict[int, np.ndarray]:
        """
        Returns a dict cv -> masked array (N,F,T).

        For mask_method == 'cells':
            - Select cells by importance per sample:
                direction='top'  -> most salient cells
                direction='bottom' -> least salient cells
            - mode='remove': set selected cells to baseline (zero/mean)
            - mode='keep'  : keep ONLY selected cells; set all others to baseline
        For legacy methods ('std','end','end_fit'): preserves original behavior when k/direction/mode not used.
        """
        direction = direction.lower()
        mode = mode.lower()
        if direction not in ("top", "bottom"):
            raise ValueError("direction must be 'top' or 'bottom'")
        if mode not in ("remove", "keep"):
            raise ValueError("mode must be 'remove' or 'keep'")

        new_xs, start_masked_count, all_masked_count, feature_masked = {}, {}, {}, {}
        N, F, T = x_test.shape

        for cv, imp in importance_scores.items():
            imp = aggregate_scores(imp, self.aggregate_method)  # (N,F,T)
            if self.absolutize:
                imp = np.abs(imp)

            new_x = x_test.copy()
            masked = np.zeros_like(new_x, dtype=bool)
            start_masked = np.zeros_like(new_x, dtype=bool)

            if self.mask_method == "cells":
                # ----- simple cell-wise masking -----
                kk = self.top if k is None else k
                sel = self._select_cells_per_sample(imp, kk, direction=direction)  # True for selected cells

                # Baselines
                if self.substitution == "zero":
                    baseline = np.zeros((N, F, 1), dtype=new_x.dtype)  # broadcast along time when needed
                else:  # 'mean' per-sample, per-feature temporal mean
                    mean_ft = new_x.mean(axis=2, keepdims=True)  # (N,F,1)
                    baseline = mean_ft

                if mode == "remove":
                    # set selected cells to baseline value at those time indices
                    # Need full (N,F,T) baseline to index per-cell; expand
                    if self.substitution == "zero":
                        new_x[sel] = 0.0
                    else:
                        # per (N,F,T) baseline from (N,F,1)
                        base_full = np.repeat(baseline, T, axis=2)
                        new_x[sel] = base_full[sel]
                    masked[sel] = True
                    start_masked[sel] = True  # in cell-mode, start==all
                else:  # mode == 'keep'
                    # keep selected cells, mask the complement
                    keep = sel
                    comp = ~keep
                    if self.substitution == "zero":
                        new_x[comp] = 0.0
                    else:
                        base_full = np.repeat(baseline, T, axis=2)
                        new_x[comp] = base_full[comp]
                    masked[comp] = True
                    start_masked[comp] = True

            else:
                # ----- legacy behavior: your original masking strategies -----
                # (unchanged code path, supports global/local/top% with carry-forward, etc.)
                coordinate_list = self._generate_arg_sort(imp, randomize_ties=True)
                if self.mask_method in ["std", "end"]:
                    if not self.local:
                        num_feature_time_drop = int(len(coordinate_list) * self.top)
                        coordinate_list = coordinate_list[:num_feature_time_drop]
                        for sample_id, feature_id, time_id in coordinate_list:
                            if imp[sample_id, feature_id, time_id] <= self.importance_threshold:
                                break
                            if masked[sample_id, feature_id, time_id]:
                                continue
                            end_time, new_x[sample_id, feature_id, :] = self._carry_forward(
                                time_id, new_x[sample_id, feature_id, :], self.mask_method
                            )
                            masked[sample_id, feature_id, time_id:end_time] = True
                            start_masked[sample_id, feature_id, time_id] = True
                    else:
                        num_feature_time = new_x.shape[1] * (new_x.shape[2] - self.min_time)
                        for sample_id in range(new_x.shape[0]):
                            start = sample_id * num_feature_time
                            num_masked_total = 0
                            num_masked = 0
                            for cur in range(num_feature_time):
                                sample_index, feature_id, time_id = coordinate_list[start + cur]
                                if sample_index != sample_id:
                                    raise RuntimeError("Failed sanity check!")
                                balance_condition = self.balanced and num_masked_total >= self.top
                                num_drop_condition = num_masked >= self.top
                                threshold_condition = imp[sample_id, feature_id, time_id] <= self.importance_threshold
                                if threshold_condition or balance_condition or num_drop_condition:
                                    break
                                if masked[sample_index, feature_id, time_id]:
                                    continue
                                end_ts, new_x[sample_id, feature_id, :] = self._carry_forward(
                                    time_id, new_x[sample_id, feature_id, :], self.mask_method
                                )
                                masked[sample_id, feature_id, time_id:end_ts] = True
                                start_masked[sample_id, feature_id, time_id] = True
                                num_masked_total += end_ts - time_id
                                num_masked += 1
                elif self.mask_method == "end_fit":
                    # (unchanged legacy FIT-block)
                    for i, x in enumerate(new_x):
                        if not self.local:
                            q = np.percentile(imp[:, :, self.min_time:], 100 - self.top * 100)
                            min_t_feat = [
                                np.min(np.where(imp[i, f, self.min_time:] >= q)[0]) if
                                len(np.where(imp[i, f, self.min_time:] >= q)[0]) > 0 else
                                x.shape[-1] - self.min_time - 1 for f in range(F)]
                            for f in range(imp[i].shape[0]):
                                x[f, min_t_feat[f] + self.min_time:] = x[f, min_t_feat[f] + self.min_time - 1]
                                masked[i, f, min_t_feat[f] + self.min_time:] = True
                                start_masked[i, f, min_t_feat[f] + self.min_time] = True
                        else:
                            for _ in range(self.top):
                                imp_idx = np.unravel_index(imp[i, :, self.min_time:].argmax(),
                                                           imp[i, :, self.min_time:].shape)
                                imp[i, imp_idx[0], imp_idx[1] + self.min_time:] = -1
                                x[imp_idx[0], imp_idx[1] + self.min_time:] = x[imp_idx[0], imp_idx[1] + self.min_time - 1]
                                masked[i, imp_idx[0], imp_idx[1] + self.min_time:] = True
                                start_masked[i, imp_idx[0], imp_idx[1] + self.min_time] = True

            # accounting (same shape as before)
            start_masked_count[cv] = np.sum(start_masked, axis=0)
            all_masked_count[cv]   = np.sum(masked, axis=0)
            feature_masked[cv]     = np.sum(np.sum(start_masked, axis=1) > 0, axis=0)

            new_xs[cv] = new_x

        self.start_masked_count = start_masked_count
        self.all_masked_count   = all_masked_count
        self.feature_masked     = feature_masked
        return new_xs

    def get_name(self) -> str:
        if self.mask_method == "cells":
            # kstr = f"{int(self.top)}" if isinstance(self.top, int) else f"{int(self.top*100)}p"
            return f"cells_{self.substitution}_{self.aggregate_method}"
        # legacy naming (unchanged)
        if self.local:
            if self.balanced:
                return f"bal{int(self.top)}_{self.mask_method}_{self.aggregate_method}"
            return f"top{int(self.top)}_{self.mask_method}_{self.aggregate_method}"
        return f"globaltop{int(self.top * 100)}_{self.mask_method}_{self.aggregate_method}"

    def _generate_arg_sort(self, scores, randomize_ties=True):
        """
        Returns a list of coordinates that is the argument of the sorting. In descending order.
        If local is True, the list of coordinates will be sorted within each sample. i.e.,
        the first (num_features * num_times) coordinates would always correspond to the first
        sample, the next (num_features * num_times) coordinates would correspond to the second
        sample, etc.

        Args:
            scores:
               Importance scores of shape (num_samples, num_features, num_times)
            min_time:
               The minimum timesteps to sort.
            local:
               Indicates whether the sorting is local or global. i.e. along all axes, or just the
               feature and time axes.
            randomize_ties:
               If there is a tie in the scores (which happens very often in Dynamask), we randomly
               permute the coordinates across the tie.

        Returns:
            A array of shape (all_coordinate_length, 3), for each row is the coordinate.
        """
        truncated_scores = scores[:, :, self.min_time :]
        if self.local:
            flattened_scores = truncated_scores.reshape(scores.shape[0], -1)
            argsorted_ravel_local = np.argsort(flattened_scores)[:, ::-1]
            if randomize_ties:
                self._shuffle_ties(argsorted_ravel_local, flattened_scores)
            feature_index = argsorted_ravel_local // truncated_scores.shape[2]
            time_index = (argsorted_ravel_local % truncated_scores.shape[2]) + self.min_time
            arange = np.arange(scores.shape[0]).reshape(-1, 1).repeat(feature_index.shape[1], 1)
            coordinate_list = np.stack([arange, feature_index, time_index]).reshape(
                3, -1
            )  # (3, all_coordinate_length)
            return coordinate_list.transpose()
        else:
            flattened_scores = truncated_scores.ravel()
            argsorted_ravel_global = np.argsort(flattened_scores)[::-1]
            if randomize_ties:
                self._shuffle_ties_global(argsorted_ravel_global, flattened_scores)
            coordinate_list = np.stack(
                np.unravel_index(argsorted_ravel_global, truncated_scores.shape)
            )  # (3, all_coordinate_length)
            coordinate_list[2, :] += self.min_time
            return coordinate_list.transpose()

    def _shuffle_ties_global(self, argsorted_ravel_global, flattened_scores):
        sorted_scores = flattened_scores[argsorted_ravel_global]
        repeated = np.r_[False, sorted_scores[1:] == sorted_scores[:-1]]
        indices = np.where(np.diff(repeated))[0]
        if len(indices) % 2 == 1:
            indices = np.r_[indices, len(sorted_scores) - 1]
        indices = indices.reshape(-1, 2)
        indices[:, 1] += 1
        rng = np.random.default_rng(self.seed)
        for repeated_index in indices:
            from_index, to_index = repeated_index
            rng.shuffle(argsorted_ravel_global[from_index:to_index])

    def _shuffle_ties(self, argsorted_ravel_local, flattened_scores):
        sorted_scores = np.take_along_axis(flattened_scores, argsorted_ravel_local, axis=1)
        repeated = np.concatenate(
            [
                np.zeros((len(sorted_scores), 1), dtype=bool),
                sorted_scores[:, 1:] == sorted_scores[:, :-1],
            ],
            axis=1,
        )
        indices = np.where(np.diff(repeated, axis=1))
        previous_sample_id = -1
        left = -1
        rng = np.random.default_rng(self.seed)
        for sample_id, x in zip(*indices):
            if sample_id != previous_sample_id:
                # new sample seen
                if left != -1:
                    # still have right bound left.
                    right = sorted_scores.shape[1]
                    rng.shuffle(argsorted_ravel_local[previous_sample_id, left:right])
                left = x
            else:
                # same sample
                if left != -1:
                    rng.shuffle(argsorted_ravel_local[previous_sample_id, left : x + 1])
                    left = -1
                else:
                    left = x
            previous_sample_id = sample_id
        if left != -1:
            right = sorted_scores.shape[1]
            rng.shuffle(argsorted_ravel_local[previous_sample_id, left:right])

    def _carry_forward(self, timestep, time_series, mask):
        assert len(time_series.shape) == 1
        ts_length = time_series.shape[0]

        assert timestep != 0, "Carry forward is not defined at index 0"

        if mask == "std":
            threshold = np.std(time_series)
            old = time_series[timestep]
            segment = np.abs(time_series[timestep:] - old) > threshold
            over = np.where(segment)[0]
            new_timestep = ts_length if len(over) == 0 else over[0] + timestep
        elif mask == "end":
            new_timestep = ts_length
        else:
            raise NotImplementedError(f"Mask method {mask} not recognized for carry forward")

        time_series[timestep:new_timestep] = time_series[timestep - 1]
        return new_timestep, time_series