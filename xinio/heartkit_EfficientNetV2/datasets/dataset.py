import functools
import logging
import os

import numpy as np
import numpy.typing as npt
import sklearn
import tensorflow as tf

from ..utils import load_pkl, save_pkl
from .defines import PatientGenerator, Preprocessor, SampleGenerator
from .utils import create_dataset_from_data

logger = logging.getLogger(__name__)


def default_preprocess(x: npt.NDArray) -> npt.NDArray:
    """Default identity preprocessing."""
    return x


class HKDataset:
    """HeartKit dataset base class"""

    target_rate: int
    ds_path: os.PathLike
    task: str
    frame_size: int
    spec: tuple[tf.TensorSpec, tf.TensorSpec]
    class_map: dict[int, int]

    def __init__(
        self,
        ds_path: os.PathLike,
        task: str,
        frame_size: int,
        target_rate: int,
        spec: tuple[tf.TensorSpec, tf.TensorSpec],
        class_map: dict[int, int] | None = None,
    ) -> None:
        """HeartKit dataset base class"""
        self.ds_path = ds_path
        self.task = task
        self.frame_size = frame_size
        self.target_rate = target_rate
        self.spec = spec
        self.class_map = class_map or {}

    #############################################
    # !! Below must be implemented in subclass !!
    #############################################

    @property
    def cachable(self) -> bool:
        """If dataset supports file caching."""
        return True

    @property
    def sampling_rate(self) -> int:
        """Sampling rate in Hz"""
        return 0

    def get_train_patient_ids(self) -> npt.NDArray:
        """Get training patient IDs

        Returns:
            npt.NDArray: patient IDs
        """
        raise NotImplementedError()

    def get_test_patient_ids(self) -> npt.NDArray:
        """Get patient IDs reserved for testing only

        Returns:
            npt.NDArray: patient IDs
        """
        raise NotImplementedError()

    def task_data_generator(
        self,
        patient_generator: PatientGenerator,
        samples_per_patient: int | list[int] = 1,
    ) -> SampleGenerator:
        """Task-level data generator.

        Args:
            patient_generator (PatientGenerator): Patient data generator
            samples_per_patient (int | list[int], optional): # samples per patient. Defaults to 1.

        Returns:
            SampleGenerator: Sample data generator
        """
        raise NotImplementedError()

    def download(self, num_workers: int | None = None, force: bool = False):
        """Download dataset

        Args:
            num_workers (int | None, optional): # parallel workers. Defaults to None.
            force (bool, optional): Force redownload. Defaults to False.
        """
        raise NotImplementedError()

    def uniform_patient_generator(
        self,
        patient_ids: npt.NDArray,
        repeat: bool = True,
        shuffle: bool = True,
    ) -> PatientGenerator:
        """Yield data uniformly for each patient in the array.

        Args:
            patient_ids (npt.NDArray): Array of patient ids
            repeat (bool, optional): Whether to repeat generator. Defaults to True.
            shuffle (bool, optional): Whether to shuffle patient ids. Defaults to True.

        Returns:
            PatientGenerator: Patient generator

        Yields:
            Iterator[PatientGenerator]
        """
        raise NotImplementedError()

    #############################################
    # !! Above must be implemented in subclass !!
    #############################################

    def load_train_datasets(
        self,
        train_patients: float | None = None,
        val_patients: float | None = None,
        train_pt_samples: int | list[int] | None = None,
        val_pt_samples: int | list[int] | None = None,
        val_size: int | None = None,
        val_file: str | None = None,
        preprocess: Preprocessor | None = None,
        num_workers: int = 1,
    ) -> tuple[tf.data.Dataset, tf.data.Dataset]:
        """Load training and validation TF datasets

        Args:
            train_patients (float | None, optional): # or proportion of train patients. Defaults to None.
            val_patients (float | None, optional): # or proportion of val patients. Defaults to None.
            train_pt_samples (int | list[int] | None, optional): # samples per patient for training. Defaults to None.
            val_pt_samples (int | list[int] | None, optional): # samples per patient for validation. Defaults to None.
            val_size (int | None, optional): Validation size. Defaults to 200*len(val_patient_ids).
            val_file (str | None, optional): Path to existing pickled validation file. Defaults to None.
            num_workers (int, optional): # of parallel workers. Defaults to 1.

        Returns:
            tuple[tf.data.Dataset, tf.data.Dataset]: Training and validation datasets
        """

        logger.info(f"Samples per patient in the training is: {train_pt_samples}")
        if val_patients is not None and val_patients >= 1:
            val_patients = int(val_patients)

        train_pt_samples = train_pt_samples or 100
        if val_pt_samples is None:
            val_pt_samples = train_pt_samples

        # Get train patients
        train_patient_ids = self.get_train_patient_ids()

        # Use subset of training patients
        if train_patients is not None:
            num_pts = int(train_patients) if train_patients > 1 else int(train_patients * len(train_patient_ids))
            train_patient_ids = train_patient_ids[:num_pts]
        # END IF

        # Use existing validation data
        if self.cachable and val_file and os.path.isfile(val_file):
            logger.info(f"Loading validation data from file {val_file}")
            val = load_pkl(val_file)
            val_ds = create_dataset_from_data(val["x"], val["y"], self.spec)
            val_patient_ids = val["patient_ids"]
            train_patient_ids = np.setdiff1d(train_patient_ids, val_patient_ids)
        else:
            logger.info("Splitting patients into train and validation")
            # get the proper id for train and validation patient id, we are doing it per patient
            train_patient_ids, val_patient_ids = self._split_train_test_patients(
                patient_ids=train_patient_ids, test_size=val_patients
            )
            if val_size is None:
                val_size = val_pt_samples * (len(val_patient_ids) - 1)
            
            # this can be stuck if your filter parameters not correct
            logger.info(f"Collecting {val_size} validation samples")
            val_ds = self._parallelize_dataset(
                patient_ids=val_patient_ids,
                samples_per_patient=val_pt_samples,
                preprocess=preprocess,
                repeat=False,
                num_workers=num_workers,
            )
            
            val_x, val_y = next(val_ds.batch(val_size).as_numpy_iterator())
            val_ds = create_dataset_from_data(val_x, val_y, self.spec)

            # Cache validation set
            if self.cachable and val_file:
                logger.info(f"Caching the validation set in {val_file}")
                os.makedirs(os.path.dirname(val_file), exist_ok=True)
                save_pkl(val_file, x=val_x, y=val_y, patient_ids=val_patient_ids)
            # END IF
        # END IF

        logger.info("Building train dataset")
        train_ds = self._parallelize_dataset(
            patient_ids=train_patient_ids,
            samples_per_patient=train_pt_samples,
            preprocess=preprocess,
            repeat=True,
            num_workers=num_workers,
        )
        return train_ds, val_ds

    def load_test_dataset(
        self,
        test_patients: float | None = None,
        test_pt_samples: int | list[int] | None = None,
        repeat: bool = True,
        preprocess: Preprocessor | None = None,
        num_workers: int = 1,
    ) -> tf.data.Dataset:
        """Load testing datasets

        Args:
            test_patients (float | None, optional): # or proportion of test patients. Defaults to None.
            test_pt_samples (int | None, optional): # samples per patient for testing. Defaults to None.
            repeat (bool, optional): Restart generator when dataset is exhausted. Defaults to True.
            num_workers (int, optional): # of parallel workers. Defaults to 1.

        Returns:
            tf.data.Dataset: Test dataset
        """
        test_patient_ids = self.get_test_patient_ids()

        if test_patients is not None:
            num_pts = int(test_patients) if test_patients > 1 else int(test_patients * len(test_patient_ids))
            test_patient_ids = test_patient_ids[:num_pts]

        test_ds = self._parallelize_dataset(
            patient_ids=test_patient_ids,
            samples_per_patient=test_pt_samples,
            preprocess=preprocess,
            repeat=repeat,
            num_workers=num_workers,
        )
        return test_ds

    @tf.function
    def _parallelize_dataset(
        self,
        patient_ids: npt.NDArray,
        samples_per_patient: int | list[int] = 100,
        preprocess: Preprocessor | None = None,
        repeat: bool = False,
        num_workers: int = 1,
    ) -> tf.data.Dataset:
        """Generates datasets for given task in parallel using TF `interleave`

        Args:
            patient_ids (npt.NDArray): List of patient IDs.
            samples_per_patient (int | list[int], optional): # Samples per patient and class. Defaults to 100.
            repeat (bool, optional): Should data generator repeat. Defaults to False.
            num_workers (int, optional): Number of parallel workers. Defaults to 1.

        Returns:
            tf.data.Dataset: Parallelize dataset
        """

        def _make_train_dataset(i, split):
            return self._create_dataset_from_generator(
                patient_ids=patient_ids[i * split : (i + 1) * split],
                samples_per_patient=samples_per_patient,
                repeat=repeat,
                preprocess=preprocess,
            )

        if num_workers > len(patient_ids):
            num_workers = len(patient_ids)
        split = len(patient_ids) // num_workers
        # TODO: get the signal and the signal label here
        datasets = [_make_train_dataset(i, split) for i in range(num_workers)]
        if num_workers <= 1:
            return datasets[0]

        return tf.data.Dataset.from_tensor_slices(datasets).interleave(
            lambda x: x,
            cycle_length=num_workers,
            num_parallel_calls=tf.data.AUTOTUNE,
        )

    def _split_train_test_patients(self, patient_ids: npt.NDArray, test_size: float) -> list[list[int]]:
        """Perform train/test split on patients for given task.
        NOTE: We only perform inter-patient splits and not intra-patient.

        Args:
            patient_ids (npt.NDArray): Patient Ids
            test_size (float): Test size

        Returns:
            list[list[int]]: Train and test sets of patient ids
        """
        return sklearn.model_selection.train_test_split(patient_ids, test_size=test_size)

    # TODO: align the signal and the signal label here
    def _create_dataset_from_generator(
        self,
        patient_ids: npt.NDArray,
        samples_per_patient: int | list[int] = 100,
        preprocess: Preprocessor | None = None,
        repeat: bool = True,
    ) -> tf.data.Dataset:
        """Creates TF dataset generator for task.

        Args:
            patient_ids (npt.NDArray): Patient IDs
            samples_per_patient (int | list[int], optional): Samples per patient. Defaults to 100.
            repeat (bool, optional): Repeat. Defaults to True.

        Returns:
            tf.data.Dataset: Dataset generator
        """
        ds_gen = functools.partial(self._dataset_sample_generator, preprocess=preprocess)
        dataset = tf.data.Dataset.from_generator(
            generator=ds_gen,
            output_signature=self.spec,
            args=(patient_ids, samples_per_patient, repeat),
        )
        options = tf.data.Options()
        options.experimental_distribute.auto_shard_policy = tf.data.experimental.AutoShardPolicy.FILE
        dataset = dataset.with_options(options)
        return dataset
    
    
    # TODO: reverse order
    def _dataset_sample_generator(
        self,
        patient_ids: npt.NDArray,
        samples_per_patient: int | list[int] = 100,
        repeat: bool = True,
        preprocess: Preprocessor | None = None,
    ) -> SampleGenerator:
        """Internal sample generator for task.

        Args:
            patient_ids (npt.NDArray): Patient IDs
            samples_per_patient (int | list[int], optional): Samples per patient. Defaults to 100.
            repeat (bool, optional): Repeat. Defaults to True.

        Returns:
            SampleGenerator: Task sample generator
        """
        patient_generator = self.uniform_patient_generator(patient_ids, repeat=repeat)
        data_generator = self.task_data_generator(
            patient_generator,
            samples_per_patient=samples_per_patient,
        )
        preprocess_fn = preprocess if preprocess else default_preprocess
        num_classes = len(set(self.class_map.values()))
        feat_shape = tuple(self.spec[0].shape)

        data_generator = map(
            lambda x_y: (
                preprocess_fn(x_y[0]).reshape(feat_shape),
                x_y[0] if num_classes <= 1 else tf.one_hot(x_y[1], num_classes), # x_y[1] should be the corresponding label
            ),
            data_generator,
        )

        return data_generator
