"""
ComparativeEncoder module, trains a model comparatively using distances.
"""
import time
import multiprocessing as mp

import numpy as np
import tensorflow as tf
from keras import backend as K
from tqdm import tqdm

from . import distance
from ._output_mgmt import suppress_output


def correlation_coefficient_loss(y_true, y_pred):
    """
    Correlation coefficient loss function for ComparativeEncoder.
    """
    x = y_true
    y = y_pred
    mx = K.mean(x)
    my = K.mean(y)
    xm, ym = x - mx, y - my
    r_num = K.sum(tf.multiply(xm, ym))
    r_den = K.sqrt(tf.multiply(K.sum(K.square(xm)), K.sum(K.square(ym))))
    r = r_num / r_den

    r = K.maximum(K.minimum(r, 1.0), -1.0)
    return 1 - r


class DistanceLayer(tf.keras.layers.Layer):
    """
    This layer computes the distance between its two prior layers. Necessary for comparative models.
    """
    # pylint: disable=missing-function-docstring
    def call(self, a, b):
        return tf.reduce_sum(tf.square(a - b), -1)


class ComparativeEncoder:
    """
    Generic comparative encoder that can fit to data and transform sequences.
    """
    def __init__(self, encoder: tf.keras.Model, dist=None, strategy=None, quiet=False):
        """
        @param encoder: TensorFlow model that must support .train() and .__call__() at minimum.
        .predict() required for progress bar when transforming data.
        @param dist: distance metric to use when comparing two sequences.
        """
        self.quiet = quiet
        input_shape = encoder.layers[0].output_shape[0][1:]
        input_dtype = encoder.layers[0].dtype
        self.repr_size = encoder.layers[-1].output_shape[1:]
        self.depth = len(encoder.layers)
        self.encoder = encoder
        self.distance = dist or distance.Distance()
        self.strategy = strategy or tf.distribute.get_strategy()

        with self.strategy.scope():
            inputa = tf.keras.layers.Input(input_shape, name='input_a', dtype=input_dtype)
            inputb = tf.keras.layers.Input(input_shape, name='input_b', dtype=input_dtype)
            distances = DistanceLayer()(
                encoder(inputa),
                encoder(inputb),
            )
            self.comparative_model = tf.keras.Model(inputs=[inputa, inputb], outputs=distances)
            self.comparative_model.compile(optimizer='adam', loss=correlation_coefficient_loss)

    @classmethod
    def from_model_builder(cls, obj, dist=None, quiet=False, **compile_params):
        """
        Factory function from a ModelBuilder object.
        @param obj: ModelBuilder object.
        @param dist: distance metric to use when comparing two sequences.
        @param quiet: whether to silence object
        @param compile_params: passed into ModelBuilder.compile()
        @return ComparativeEncoder: new object
        """
        model = obj.compile(**compile_params)
        return cls(model, dist=dist, strategy=obj.strategy, quiet=quiet)

    def _randomized_epoch(self, data: np.ndarray, distance_on: np.ndarray, jobs: int,
                          chunksize: int, batch_size: int):
        """
        Train a single randomized epoch on data and distance_on.
        @param data: data to train model on.
        @param distance_on: data to use for distance computations.
        @param jobs: number of CPU jobs to use.
        @param chunksize: chunksize for Python multiprocessing.
        @param batch_size: batch size for TensorFlow.
        """
        rng = np.random.default_rng()
        p1 = rng.permutation(data.shape[0])
        x1 = data[p1]
        y1 = distance_on[p1]
        p2 = rng.permutation(data.shape[0])
        x2 = data[p2]
        y2 = distance_on[p2]

        with mp.Pool(jobs) as p:
            y = np.array(list(tqdm(p.imap(self.distance.transform, zip(y1, y2),
                                          chunksize=chunksize), total=y1.shape[0])))
        y = self.distance.postprocessor(y)  # Vectorized transformations are applied here

        train_data = tf.data.Dataset.from_tensor_slices(({'input_a': x1, 'input_b': x2}, y))
        train_data = train_data.batch(batch_size)

        options = tf.data.Options()
        options.experimental_distribute.auto_shard_policy = \
            tf.data.experimental.AutoShardPolicy.DATA
        train_data = train_data.with_options(options)

        self.comparative_model.fit(train_data, epochs=1)

    def fit(self, data: np.ndarray, distance_on=None, batch_size=100, epochs=10, jobs=1,
            chunksize=1):
        """
        Fit the ComparativeEncoder to the given data.
        @param data: np.ndarray to train on.
        @param distance_on: np.ndarray of data to use for distance computations. Allows for distance
        to be based on secondary properties of each sequence, or on a string representation of the
        sequence (e.g. for alignment comparison methods).
        @param batch_size: batch size for TensorFlow.
        @param epochs: epochs to train for.
        @param jobs: number of CPU jobs to use for distance calculations (not GPU optimized).
        @param chunksize: chunksize for Python multiprocessing.
        """
        distance_on = distance_on if distance_on is not None else data
        def epoch():
            return self._randomized_epoch(data, distance_on, jobs, chunksize, batch_size)

        for i in range(epochs):
            start = time.time()
            if self.quiet:
                suppress_output(epoch)
            else:
                print(f'Epoch {i + 1}:')
                epoch()
                print(f'Epoch time: {time.time() - start}')

    def transform(self, data: np.ndarray, progress=True, batch_size=100) -> np.ndarray:
        """
        Transform the given data into representations using trained model.

        @param data: np.ndarray containing all sequences to transform.
        @param progress: Shows a progress bar. progress=True slows down execution due to batching
        in TensorFlow's .predict() as opposed to .__call__(). batch_size argument is also required
        for this feature.
        @param batch_size: Batch size for .predict(), not required if not using progress.
        @return np.ndarray: Representations for all sequences in data.
        """
        if progress and not self.quiet:
            return self.encoder.predict(data)
        return self.encoder(data, batch_size=batch_size)

    def save(self, path: str):
        """
        Save the encoder model to the given path.
        @param path: path to save to.
        """
        self.encoder.save(path)

    @classmethod
    def load(cls, path: str, dist=None):
        """
        Load an encoder model and create a new ComparativeEncoder.
        @param path: path where model is saved.
        @param dist: distance metric to use for new ComparativeEncoder.
        @return: ComparativeEncoder object
        """
        custom_objects = {'correlation_coefficient_loss': correlation_coefficient_loss}
        with tf.keras.utils.custom_object_scope(custom_objects):
            embeddings = tf.keras.models.load_model(path)
        return cls(embeddings, dist)

    def summary(self):
        """
        Prints a summary of the encoder.
        """
        self.encoder.summary()

