# Copyright 2021 The Trieste Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from abc import ABC
from typing import Callable, List, cast

import gpflow.kernels

try:
    from gpflux.layers.basis_functions.fourier_features import RandomFourierFeaturesCosine as RFF
except (ModuleNotFoundError, ImportError):
    # temporary support for gpflux 0.2.3
    from gpflux.layers.basis_functions import RandomFourierFeatures as RFF

import tensorflow as tf
from gpflow.inducing_variables import InducingPoints
from gpflux.layers import GPLayer, LatentVariableLayer
from gpflux.math import compute_A_inv_b
from gpflux.models import DeepGP
from gpflux.sampling.sample import Sample

from ...types import TensorType
from ...utils import DEFAULTS, flatten_leading_dims
from ..interfaces import (
    ReparametrizationSampler,
    TrajectoryFunction,
    TrajectoryFunctionClass,
    TrajectorySampler,
)
from .interface import GPfluxPredictor


def sample_consistent_lv_layer(layer: LatentVariableLayer) -> Sample:
    r"""
    Returns a :class:`~gpflux.sampling.sample.Sample` object which allows for consistent sampling
    (i.e. function samples) from a given :class:`~gpflux.layers.LatentVariableLayer`.

    :param layer: The GPflux latent variable layer to obtain samples from.
    :return: The GPflux sampling object which can be called to obtain consistent samples.
    """

    class SampleLV(Sample):
        def __call__(self, X: TensorType) -> tf.Tensor:
            sample = layer.prior.sample()
            batch_shape = tf.shape(X)[:-1]
            sample_rank = tf.rank(sample)
            for _ in range(len(batch_shape)):
                sample = tf.expand_dims(sample, 0)
            sample = tf.tile(
                sample, tf.concat([batch_shape, tf.ones(sample_rank, dtype="int32")], -1)
            )
            return layer.compositor([X, sample])

    return SampleLV()


def sample_dgp(model: DeepGP) -> TrajectoryFunction:
    r"""
    Builds a :class:`TrajectoryFunction` that can be called for a :class:`~gpflux.models.DeepGP`,
    which will give consistent (i.e. function) samples from a deep GP model.

    :param model: The GPflux deep GP model to sample from.
    :return: The trajectory function that gives a consistent sample function from the model.
    """
    function_draws = []
    for layer in model.f_layers:
        if isinstance(layer, GPLayer):
            function_draws.append(layer.sample())
        elif isinstance(layer, LatentVariableLayer):
            function_draws.append(sample_consistent_lv_layer(layer))
        else:
            raise NotImplementedError(f"Sampling not implemented for {type(layer)}")

    class ChainedSample(Sample):
        def __call__(self, X: TensorType) -> tf.Tensor:
            for f in function_draws:
                X = f(X)
            return X

    return ChainedSample().__call__


class DeepGaussianProcessTrajectorySampler(TrajectorySampler[GPfluxPredictor]):
    r"""
    This sampler provides trajectory samples from a :class:`GPfluxPredictor`\ 's predictive
    distribution, for :class:`GPfluxPredictor`\s with an underlying
    :class:`~gpflux.models.DeepGP` model.
    """

    def __init__(self, model: GPfluxPredictor):
        """
        :param model: The model to sample from.
        :raise ValueError: If the model is not a :class:`GPfluxPredictor`, or its underlying
            ``model_gpflux`` is not a :class:`~gpflux.models.DeepGP`.
        """
        if not isinstance(model, GPfluxPredictor):
            raise ValueError(
                f"Model must be a gpflux.interface.GPfluxPredictor, received {type(model)}"
            )

        super().__init__(model)

        self._model_gpflux = model.model_gpflux

        if not isinstance(self._model_gpflux, DeepGP):
            raise ValueError(
                f"GPflux model must be a gpflux.models.DeepGP, received {type(self._model_gpflux)}"
            )

    def get_trajectory(self) -> TrajectoryFunction:
        """
        Generate an approximate function draw (trajectory) by using the GPflux sampling
        functionality. These trajectories are differentiable with respect to the input, so can be
        used to e.g. find the minima of Thompson samples.

        :return: A trajectory function representing an approximate trajectory from the deep Gaussian
            process, taking an input of shape `[N, D]` and returning shape `[N, 1]`.
        """

        return sample_dgp(self._model_gpflux)


class DeepGaussianProcessReparamSampler(ReparametrizationSampler[GPfluxPredictor]):
    r"""
    This sampler employs the *reparameterization trick* to approximate samples from a
    :class:`GPfluxPredictor`\ 's predictive distribution, when the :class:`GPfluxPredictor` has
    an underlying :class:`~gpflux.models.DeepGP`.
    """

    def __init__(self, sample_size: int, model: GPfluxPredictor):
        """
        :param sample_size: The number of samples for each batch of points. Must be positive.
        :param model: The model to sample from.
        :raise ValueError (or InvalidArgumentError): If ``sample_size`` is not positive, if the
            model is not a :class:`GPfluxPredictor`, of if its underlying ``model_gpflux`` is not a
            :class:`~gpflux.models.DeepGP`.
        """
        if not isinstance(model, GPfluxPredictor):
            raise ValueError(
                f"Model must be a gpflux.interface.GPfluxPredictor, received {type(model)}"
            )

        super().__init__(sample_size, model)

        self._model_gpflux = model.model_gpflux

        if not isinstance(self._model_gpflux, DeepGP):
            raise ValueError(
                f"GPflux model must be a gpflux.models.DeepGP, received {type(self._model_gpflux)}"
            )

        # Each element of _eps_list is essentially a lazy constant. It is declared and assigned an
        # empty tensor here, and populated on the first call to sample
        self._eps_list = [
            tf.Variable(tf.ones([sample_size, 0], dtype=tf.float64), shape=[sample_size, None])
            for _ in range(len(self._model_gpflux.f_layers))
        ]

    def sample(self, at: TensorType, *, jitter: float = DEFAULTS.JITTER) -> TensorType:
        """
        Return approximate samples from the `model` specified at :meth:`__init__`. Multiple calls to
        :meth:`sample`, for any given :class:`DeepGaussianProcessReparamSampler` and ``at``, will
        produce the exact same samples. Calls to :meth:`sample` on *different*
        :class:`DeepGaussianProcessReparamSampler` instances will produce different samples.

        :param at: Where to sample the predictive distribution, with shape `[N, D]`, for points
            of dimension `D`.
        :param jitter: The size of the jitter to use when stabilizing the Cholesky
            decomposition of the covariance matrix.
        :return: The samples, of shape `[S, N, L]`, where `S` is the `sample_size` and `L` is
            the number of latent model dimensions.
        :raise ValueError (or InvalidArgumentError): If ``at`` has an invalid shape or ``jitter``
            is negative.
        """
        tf.debugging.assert_equal(len(tf.shape(at)), 2)
        tf.debugging.assert_greater_equal(jitter, 0.0)

        samples = tf.tile(tf.expand_dims(at, 0), [self._sample_size, 1, 1])
        for i, layer in enumerate(self._model_gpflux.f_layers):
            if isinstance(layer, LatentVariableLayer):
                if not self._initialized:
                    self._eps_list[i].assign(layer.prior.sample([tf.shape(samples)[:-1]]))
                samples = layer.compositor([samples, self._eps_list[i]])
                continue

            mean, var = layer.predict(samples, full_cov=False, full_output_cov=False)

            if not self._initialized:
                self._eps_list[i].assign(
                    tf.random.normal([self._sample_size, tf.shape(mean)[-1]], dtype=tf.float64)
                )

            samples = mean + tf.sqrt(var) * tf.cast(self._eps_list[i][:, None, :], var.dtype)

        if not self._initialized:
            self._initialized.assign(True)

        return samples


class DeepGaussianProcessDecoupledTrajectorySampler(TrajectorySampler[GPfluxPredictor]):
    """
    This sampler provides approximate trajectory samples using decoupled sampling (i.e. Matheron's
    rule) for GPflux DeepGP models.
    """

    def __init__(
        self,
        model: GPfluxPredictor,
        num_features: int = 1000,
    ):
        if not isinstance(model, GPfluxPredictor):
            raise ValueError(
                f"Model must be a gpflux.interface.GPfluxPredictor, received {type(model)}"
            )
        if not isinstance(model.model_gpflux, DeepGP):
            raise ValueError(
                f"GPflux model must be a gpflux.models.DeepGP, received {type(model.model_gpflux)}"
            )

        super().__init__(model)
        tf.debugging.assert_positive(num_features)
        self._num_features = num_features
        self._model_gpflux = model.model_gpflux
        self._sampling_layers = [
            DeepGaussianProcessDecoupledLayer(layer, num_features)
            for layer in self._model_gpflux.f_layers
        ]

    def __repr__(self) -> str:
        """"""
        return f"""{self.__class__.__name__}(
        {self._model!r},
        {self._num_features!r})
        """

    def get_trajectory(self) -> TrajectoryFunction:
        """
        Generate an approximate function draw (trajectory) from the deep GP model.

        :return: A trajectory function representing an approximate trajectory from the deep GP,
            taking an input of shape `[N, D]` and returning shape `[N, 1]`
        """

        return dgp_feature_decomposition_trajectory(self._sampling_layers)

    def update_trajectory(self, trajectory: TrajectoryFunction) -> TrajectoryFunction:
        """
        Efficiently update a :const:`TrajectoryFunction` to reflect an update in its underlying
        :class:`ProbabilisticModel` and resample accordingly.

        :param trajectory: The trajectory function to be updated and resampled.
        :return: The updated and resampled trajectory function.
        """

        tf.debugging.Assert(isinstance(trajectory, dgp_feature_decomposition_trajectory), [])

        cast(dgp_feature_decomposition_trajectory, trajectory).update()
        return trajectory

    def resample_trajectory(self, trajectory: TrajectoryFunction) -> TrajectoryFunction:
        """
        Efficiently resample a :const:`TrajectoryFunction` in-place to avoid function retracing
        with every new sample.

        :param trajectory: The trajectory function to be resampled.
        :return: The new resampled trajectory function.
        """
        tf.debugging.Assert(isinstance(trajectory, dgp_feature_decomposition_trajectory), [])
        cast(dgp_feature_decomposition_trajectory, trajectory).resample()
        return trajectory


class DeepGaussianProcessDecoupledLayer(ABC):
    """
    Layer that samples a decoupled trajectory from a GPflux :class:`~gpflux.layers.GPLayer` using
    Matheron's rule (:cite:`wilson2020efficiently`).
    """

    def __init__(
        self,
        layer: GPLayer,
        num_features: int = 1000,
    ):
        """
        :param layer: The layer that we wish to sample from.
        :param num_features: The number of features to use in the random feature approximation.
        :raise ValueError: If the layer is not a valid layer.
        """
        if not isinstance(layer, GPLayer):
            raise ValueError(
                f"Layers other than gpflux.layers.GPLayer are not currently supported, received"
                f"{type(layer)}"
            )

        self._num_features = num_features
        self._layer = layer

        if isinstance(layer.kernel, gpflow.kernels.SharedIndependent):
            self._kernel = layer.kernel.kernel
        else:
            self._kernel = layer.kernel

        self._feature_functions = ResampleableDecoupledDeepGaussianProcessFeatureFunctions(
            layer, num_features
        )

        self._weight_sampler = self._prepare_weight_sampler()

        self._initialized = tf.Variable(False)

        self._weights_sample = tf.Variable(
            tf.ones([0, 0, 0], dtype=tf.float64), shape=[None, None, None]
        )

        self._batch_size = tf.Variable(0, dtype=tf.int32)

    def __call__(self, x: TensorType) -> TensorType:  # [N, B, D] -> [N, B, P]
        """Call trajectory function for layer."""
        if not self._initialized:
            self._batch_size.assign(tf.shape(x)[-2])
            self.resample()
            self._initialized.assign(True)

        tf.debugging.assert_equal(
            tf.shape(x)[-2],
            self._batch_size.value(),
            message=f"""
            This trajectory only supports batch sizes of {self._batch_size}.
            If you wish to change the batch size you must get a new trajectory
            by calling the get_trajectory method of the trajectory sampler.
            """,
        )

        flat_x, unflatten = flatten_leading_dims(x)
        flattened_feature_evaluations = self._feature_functions(flat_x)
        feature_evaluations = unflatten(flattened_feature_evaluations)[
            ..., None
        ]  # [N, B, L + M, 1]

        return tf.reduce_sum(
            feature_evaluations * self._weights_sample, -2
        ) + self._layer.mean_function(
            x
        )  # [N, B, P]

    def resample(self) -> None:
        """
        Efficiently resample in-place without retracing.
        """
        self._weights_sample.assign(self._weight_sampler(self._batch_size))

    def update(self) -> None:
        """
        Efficiently update the trajectory with a new weight distribution and resample its weights.
        """
        self._weight_sampler = self._prepare_weight_sampler()
        self.resample()

    def _prepare_weight_sampler(self) -> Callable[[int], TensorType]:  # [B] -> [B, L+M, P]
        """
        Prepare the sampler function that provides samples of the feature weights for both the
        RFF and canonical feature functions, i.e. we return a function that takes in a batch size
        `B` and returns `B` samples for the weights of each of the `L` RFF features and `N`
        canonical features.
        """

        if isinstance(self._layer.inducing_variable, InducingPoints):
            inducing_points = self._layer.inducing_variable.Z  # [M, D]
        else:
            inducing_points = self._layer.inducing_variable.inducing_variable.Z  # [M, D]

        q_mu = self._layer.q_mu  # [M, P]
        q_sqrt = self._layer.q_sqrt  # [P, M, M]
        Kmm = self._kernel.K(inducing_points, inducing_points)  # [M, M]
        Kmm += tf.eye(tf.shape(inducing_points)[0], dtype=Kmm.dtype) * DEFAULTS.JITTER
        whiten = self._layer.whiten
        M, P = tf.shape(q_mu)[0], tf.shape(q_mu)[1]

        tf.debugging.assert_shapes(
            [
                (inducing_points, ["M", "D"]),
                (q_mu, ["M", "P"]),
                (q_sqrt, ["P", "M", "M"]),
                (Kmm, ["M", "M"]),
            ]
        )

        def weight_sampler(batch_size: int) -> TensorType:
            prior_weights = tf.random.normal([batch_size, self._num_features, P], dtype=tf.float64)

            u_noise_sample = tf.matmul(
                q_sqrt,  # [P, M, M]
                tf.random.normal([batch_size, P, M, 1], dtype=tf.float64),  # [B, P, M, 1]
            )  # [B, P, M, 1]
            u_sample = q_mu + tf.linalg.matrix_transpose(u_noise_sample[..., 0])  # [B, M, P]

            if whiten:
                Luu = tf.linalg.cholesky(Kmm)  # [M, M]
                u_sample = tf.matmul(Luu, u_sample)

            phi_Z = self._feature_functions(inducing_points)[:, : self._num_features]
            weight_space_prior_Z = phi_Z @ prior_weights  # [B, M, P]

            diff = u_sample - weight_space_prior_Z  # [B, M, P]
            v = compute_A_inv_b(Kmm, diff)  # [B, M, P]

            return tf.concat([prior_weights, v], axis=1)  # [B, L + M, P]

        return weight_sampler


class ResampleableDecoupledDeepGaussianProcessFeatureFunctions(RFF):  # type: ignore[misc]
    """
    A wrapper around GPflux's random Fourier feature function that allows for efficient in-place
    updating when generating new decompositions. In addition to providing Fourier features,
    this class concatenates a layer's Fourier feature expansion with evaluations of the canonical
    basis functions.
    """

    def __init__(self, layer: GPLayer, n_components: int):
        """
        :param layer: The layer that will be approximated by the feature functions.
        :param n_components: The number of features.
        """
        if not isinstance(layer, GPLayer):
            raise NotImplementedError(
                f"ResampleableDecoupledDeepGaussianProcessFeatureFunctions currently only work with"
                f"gpflux.layers.GPLayer layers, received {type(layer)} instead"
            )

        if isinstance(layer.kernel, gpflow.kernels.SharedIndependent):
            self._kernel = layer.kernel.kernel
        else:
            self._kernel = layer.kernel
        self._n_components = n_components
        super().__init__(self._kernel, self._n_components, dtype=tf.float64)

        if isinstance(layer.inducing_variable, InducingPoints):
            inducing_points = layer.inducing_variable.Z
        else:
            inducing_points = layer.inducing_variable.inducing_variable.Z

        self._canonical_feature_functions = lambda x: tf.linalg.matrix_transpose(
            self._kernel.K(inducing_points, x)
        )

        dummy_X = inducing_points[0:1, :]

        self.__call__(dummy_X)
        self.b: TensorType = tf.Variable(self.b)
        self.W: TensorType = tf.Variable(self.W)

    def resample(self) -> None:
        """
        Resample weights and biases
        """
        if not hasattr(self, "_bias_init"):
            self.b.assign(self._sample_bias(tf.shape(self.b), dtype=self._dtype))
            self.W.assign(self._sample_weights(tf.shape(self.W), dtype=self._dtype))
        else:
            self.b.assign(self._bias_init(tf.shape(self.b), dtype=self._dtype))
            self.W.assign(self._weights_init(tf.shape(self.W), dtype=self._dtype))

    def __call__(self, x: TensorType) -> TensorType:  # [N, D] -> [N, L + M]
        """
        Combine prior basis functions with canonical basic functions
        """
        fourier_feature_eval = super().__call__(x)  # [N, L]
        canonical_feature_eval = self._canonical_feature_functions(x)  # [N, M]
        return tf.concat([fourier_feature_eval, canonical_feature_eval], axis=-1)  # [N, L + M]


class dgp_feature_decomposition_trajectory(TrajectoryFunctionClass):
    r"""
    An approximate sample from a deep Gaussian process's posterior, where the samples are
    represented as a finite weighted sum of features. This class essentially takes a list of
    :class:`DeepGaussianProcessDecoupledLayer`\s and iterates through them to sample, update and
    resample.
    """

    def __init__(self, sampling_layers: List[DeepGaussianProcessDecoupledLayer]):
        """
        :param sampling_layers: Samplers corresponding to each layer of the DGP model.
        """
        self._sampling_layers = sampling_layers

    @tf.function
    def __call__(self, x: TensorType) -> TensorType:
        """Call trajectory function by looping through layers."""
        for layer in self._sampling_layers:
            x = layer(x)
        return x[..., 0]  # Assume single output

    def update(self) -> None:
        """Update the layers."""
        for layer in self._sampling_layers:
            layer.update()

    def resample(self) -> None:
        """Resample the layers."""
        for layer in self._sampling_layers:
            layer.resample()
