"""
Library for input compression before data is passed into a model.
"""
import multiprocessing as mp
import numpy as np
from tqdm import tqdm
import tensorflow as tf
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA as SKPCA, IncrementalPCA
from .kmers import KMerCounter


class Compressor:
    """
    Abstract Compressor class used for compressing input data.
    """
    def __init__(self, postcomp_len: int, quiet: bool, batch_size=0):
        self.postcomp_len = postcomp_len
        self.quiet = quiet
        self.batch_size = batch_size

    def _batch_data(self, data: np.ndarray, batch_size=None) -> tuple[np.ndarray, np.ndarray]:
        batch_size = batch_size or self.batch_size
        if batch_size == 0:
            return np.reshape(data, (1, *data.shape)), data[len(data):]
        fully_batchable_data = data[:len(data) - len(data) % batch_size]
        full_batches = np.reshape(fully_batchable_data,
                                  (-1, batch_size, *fully_batchable_data.shape[1:]))
        last_batch = data[len(data) - len(data) % batch_size:]
        return full_batches, last_batch

    def fit(self, data: np.ndarray):
        """
        Fit the compressor to the given data.
        @param data: data to fit to.
        @param quiet: whether to print output
        """

    def transform(self, data: np.ndarray) -> np.ndarray:
        """
        Compress an array of data elements.
        @param data: data to compress.
        @return np.ndarray: compressed data.
        """
        return data

    def inverse_transform(self, data: np.ndarray) -> np.ndarray:
        """
        Decodes the compressed data back to original.
        @param data: data to decode.
        @return np.ndarray: uncompressed data.
        """
        return data

    def count_kmers(self, counter: KMerCounter, seqs: list[str], batches_per_it=0) -> np.ndarray:
        """
        Counts and compresses KMers in dataset.
        """
        if not batches_per_it:  # By default, don't do any additional batching
            return self.transform(counter.kmer_counts(seqs))
        full_batches, last_batch = self._batch_data(np.array(seqs, dtype=str),
                                                    self.batch_size * batches_per_it)
        result = [self.transform(counter.kmer_counts(i)) for i in (full_batches if self.quiet
                                                                   else tqdm(full_batches))]
        final_kmers = counter.kmer_counts(last_batch)
        final_compressed = self.transform(final_kmers)
        return np.concatenate(result + [final_compressed])


class PCA(Compressor):
    """
    Use PCA to compress input data.
    """
    def __init__(self, n_components: int, quiet=False):
        self.pca = SKPCA(n_components=n_components)
        super().__init__(n_components, quiet)
        self.scaler = StandardScaler()

    def fit(self, data: np.ndarray):
        self.pca.fit(self.scaler.fit_transform(data))

    def transform(self, data: np.ndarray):
        return self.pca.transform(self.scaler.transform(data))

    def inverse_transform(self, data: np.ndarray):
        return self.pca.inverse_transform_bulk()


class IPCA(Compressor):
    """
    Use PCA to compress the input data. Supports parallelization over multiple CPUs.
    """
    def __init__(self, n_components: int, quiet=False, batch_size=None, jobs=1):
        super().__init__(n_components, quiet, batch_size or 0)
        self.jobs = jobs
        self.pca = IncrementalPCA(n_components=n_components, batch_size=batch_size)
        self.scaler = StandardScaler()

    def _mp_map_over_batches(self, fn: callable, full_batches: np.ndarray) -> np.ndarray:
        with mp.Pool(self.jobs) as p:
            it = p.imap_unordered(fn, full_batches) if self.quiet else tqdm(
                p.imap_unordered(fn, full_batches), total=len(full_batches))
            return list(it)

    def fit(self, data: np.ndarray):
        if not self.quiet:
            print(f'Fitting IPCA Compressor using CPUs: {self.jobs}...')
        data = self.scaler.fit_transform(data)
        full_batches, last_batch = self._batch_data(data)
        self._mp_map_over_batches(self.pca.partial_fit, full_batches)
        if len(last_batch) >= self.postcomp_len:
            self.pca.partial_fit(last_batch)

    def transform(self, data: np.ndarray) -> np.ndarray:
        data = self.scaler.transform(data)
        full_batches, last_batch = self._batch_data(data)
        result = self._mp_map_over_batches(self.pca.transform, full_batches)
        return np.concatenate(result + [self.pca.transform(last_batch)])

    def inverse_transform(self, data: np.ndarray) -> np.ndarray:
        full_batches, last_batch = self._batch_data(data)
        result = self._mp_map_over_batches(self.pca.inverse_transform, full_batches)
        result = np.concatenate(result + [self.pca.inverse_transform(last_batch)])
        return self.scaler.inverse_transform(result)


class AE(Compressor):
    """
    Train an autoencoder to compress the input data.
    """
    def __init__(self, inputs: tf.keras.layers.Layer, reprs: tf.keras.layers.Layer,
                 outputs: tf.keras.layers.Layer, repr_size: int, loss='mse', batch_size=100,
                 quiet=False):
        super().__init__(repr_size, quiet, batch_size)
        self.encoder = tf.keras.Model(inputs=inputs, outputs=reprs)
        self.decoder = tf.keras.Model(inputs=reprs, outputs=outputs)
        self.ae = tf.keras.Model(inputs=inputs, outputs=outputs)
        self.ae.compile(optimizer='adam', loss=loss)

    @classmethod
    def auto(cls, data: np.ndarray, repr_size: int, output_activation=None, loss='mse',
             quiet=False, batch_size=100):
        """
        Automatically generate an autoencoder based on the input data. Recommended way to create
        an AECompressor.
        """
        inputs = tf.keras.layers.Input(data.shape[1:])
        x = tf.keras.layers.Dense(data.shape[-1], activation='relu')(inputs)
        reprs = tf.keras.layers.Dense(repr_size)(x)
        x = tf.keras.layers.Dense(data.shape[-1], activation='relu')(reprs)
        outputs = tf.keras.layers.Dense(data.shape[-1], activation=output_activation)(x)
        return cls(inputs, reprs, outputs, repr_size, loss=loss, batch_size=batch_size, quiet=quiet)

    def summary(self):
        """
        Print a summary of this autoencoder.
        """
        self.ae.summary()

    def fit(self, data: np.ndarray, epoch_limit=100, patience=2, val_split=.1):
        """
        Train the autoencoder model on the given data. Uses early stopping to end training.
        """
        if not self.quiet:
            print('Training AE Compressor...')
        else:
            tf.keras.utils.disable_interactive_logging()
        self.ae.fit(data, data, epochs=epoch_limit, batch_size=self.batch_size, callbacks=[
            tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=patience),
        ], validation_split=val_split)
        if self.quiet:
            tf.keras.utils.enable_interactive_logging()

    def transform(self, data: np.ndarray) -> np.ndarray:
        return self.encoder(data) if self.quiet else \
            self.encoder.predict(data, batch_size=self.batch_size)

    def inverse_transform(self, data: np.ndarray) -> np.ndarray:
        return self.decoder(data) if self.quiet else \
            self.decoder.predict(data, batch_size=self.batch_size)

