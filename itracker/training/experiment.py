import logging

import keras.backend as K
import keras.optimizers as optimizers

import numpy as np

from rhodopsin import experiment, params

import tensorflow as tf

from ..common import config, custom_data_loader
from ..common.network import branched_autoenc_network
from ..common.network import branched_autoenc_small_network

import autoencoder_validator
import metrics
import pipelines
import validator


# Configure GPU VRAM usage.
tf_config = tf.ConfigProto()
tf_config.gpu_options.per_process_gpu_memory_fraction = 1.0
g_session = tf.Session(config=tf_config)
K.tensorflow_backend.set_session(g_session)


logger = logging.getLogger(__name__)


class Experiment(experiment.Experiment):
  """ Experiment for training the gaze predictor model. """

  def __init__(self, parser):
    """
    Args:
      parser: The CLI argument parser. """
    self.__parser = parser
    self.__args = self.__parser.parse_args()

    # Create hyperparameters.
    my_params = self.__create_hyperparameters()
    # Create status parameters.
    my_status = self.__create_status()

    # Create pipeline builder.
    face_size = config.FACE_SHAPE[:2]
    eye_size = config.EYE_SHAPE[:2]
    batch_size = self.__args.batch_size
    self.__builder = pipelines.PipelineBuilder(config.RAW_SHAPE, face_size,
                                               batch_size, eye_size=eye_size)

    super(Experiment, self).__init__(self.__args.testing_interval,
                                     hyperparams=my_params,
                                     status=my_status)

  def __create_hyperparameters(self):
    """ Creates a set of hyperparameters for the network. """
    my_params = params.HyperParams()

    # Set hyperparameters.
    my_params.add("learning_rate", self.__args.learning_rate)
    my_params.add("momentum", self.__args.momentum)
    my_params.add("training_steps", self.__args.training_steps)
    my_params.add("testing_steps", self.__args.testing_steps)

    return my_params

  def __create_status(self):
    """ Creates the status parameters for the network. """
    my_status = params.Status()

    # Add status indicators for the losses.
    my_status.add("loss", 0.0)
    my_status.add("testing_loss", 0.0)

    # Add status indicator for the accuracies.
    my_status.add("acc", 0.0)
    my_status.add("testing_acc", 0.0)

    return my_status

  def __recompile_if_needed(self):
    """ Checks if the model needs to be recompiled, and does so if necessary.
    """
    # Parameters that, if changed, require recompilation.
    forces_recomp = set(["learning_rate", "momentum"])

    # Check which parameters changed.
    my_params = self.get_params()
    changed = my_params.get_changed()
    logger.debug("Changed parameters: %s" % (changed))

    # See if we have to recompile.
    for param in changed:
      if param not in forces_recomp:
        # We don't need to recompile for this.
        continue

      # We need to recompile.
      learning_rate = my_params.get_value("learning_rate")
      momentum = my_params.get_value("momentum")

      logger.info("Recompiling with LR %f and momentum %f." % \
                  (learning_rate, momentum))

      # Set the optimizers.
      opt = optimizers.SGD(lr=learning_rate, momentum=momentum)
      self.__model.compile(optimizer=opt,
                           loss={"dots": metrics.distance_metric},
                           metrics=[metrics.distance_metric],
                           target_tensors=self.__labels)

      # We only need to compile a maximum of one time.
      break

  def __build_model(self, data_tensors):
    """ Builds the model, and loads existing weights if necessary. It also
    modifies self.__labels according to the model.
    Args:
      data_tensors: The input tensors for the model. """
    autoenc_weights = None
    clusters = None
    if (config.NET_ARCH == branched_autoenc_network.BranchedAutoencNetwork or \
        config.NET_ARCH == \
            branched_autoenc_small_network.BranchedAutoencSmallNetwork):
      # The autoencoder network takes some special parameters.
      if not self.__args.autoencoder_weights:
        raise ValueError("--autoencoder_weights is required for this network.")
      if not self.__args.clusters:
        raise ValueError("--clusters is required for this network.")

      autoenc_weights = self.__args.autoencoder_weights
      clusters = self.__args.clusters

    # Create the model.
    if self.__args.fine_tune:
      logger.info("Will now fine-tune model.")
    net = config.NET_ARCH(config.FACE_SHAPE, eye_shape=config.EYE_SHAPE,
                          data_tensors=data_tensors,
                          fine_tune=self.__args.fine_tune,
                          autoenc_model_file=autoenc_weights,
                          cluster_data=clusters)
    self.__model = net.build()

    # Prepare label data.
    self.__labels = net.prepare_labels(self.__labels)

    load_model = self.__args.model
    if load_model:
      logging.info("Loading pretrained model '%s'." % (load_model))
      self.__model.load_weights(load_model)
    elif self.__args.fine_tune:
      # Can't fine-tune without a loaded model.
      raise RuntimeError("Please specify a model with --model to fine-tune.")

  def _run_training_iteration(self):
    """ Runs a single training iteration. """
    my_params = self.get_params()
    training_steps = my_params.get_value("training_steps")

    status = self.get_status()

    # First, recompile the model if need be.
    self.__recompile_if_needed()

    # Run a training iteration.
    history = self.__model.fit(epochs=1, steps_per_epoch=training_steps)

    # Update the status parameters.
    loss = history.history["loss"][0]
    accuracy = history.history["distance_metric"][0]
    logger.debug("Training loss: %f, acc: %f" % (loss, accuracy))
    status.update("loss", loss)
    status.update("acc", accuracy)

  def _run_testing_iteration(self):
    """ Runs a single testing iteration. """
    logger.info("Running test iteration.")

    my_params = self.get_params()
    testing_steps = my_params.get_value("testing_steps")

    status = self.get_status()

    # Test the model.
    loss, accuracy = self.__model.evaluate(steps=testing_steps)

    # Update the status parameters.
    logger.info("Testing loss: %f, acc: %f" % (loss, accuracy))
    status.update("testing_loss", loss)
    status.update("testing_acc", accuracy)

    # Save the trained models.
    logger.info("Saving models.")
    self.__model.save_weights(self.__args.output)

  def train(self):
    """ Initializes and performs the entire training procedure. """
    # Build input pipelines.
    input_tensors = self.__builder.build_pipeline(self.__args.train_dataset,
                                                  self.__args.test_dataset)
    # We don't need the session number for training.
    data_tensors = input_tensors[:4]
    self.__labels = {"dots": input_tensors[-1]}

    # Create the model.
    self.__build_model(data_tensors)

    # Create a coordinator and run queues.
    coord = tf.train.Coordinator()
    threads = tf.train.start_queue_runners(coord=coord, sess=g_session)

    # Train the model.
    super(Experiment, self).train()

    coord.request_stop()
    coord.join(threads)

  def validate(self):
    """ Validates an existing model. """
    # Create the validation pipeline.
    input_tensors = \
        self.__builder.build_valid_pipeline(self.__args.valid_dataset,
                                            has_pose=self.__args.pose)
    data_tensors = input_tensors[:5]
    if self.__args.pose:
      # Include the pose data.
      data_tensors += [input_tensors[5]]
    self.__labels = {"dots": input_tensors[-1]}

    if not self.__args.model:
      # User did not tell us which model to validate.
      raise ValueError("--model must be specified.")

    # Create and run the validator.
    valid_module = validator
    if self.__args.autoencoder:
      # Use autoencoder validator.
      logger.info("Performing autoencoder validation.")
      valid_module = autoencoder_validator
    my_validator = valid_module.Validator(data_tensors, self.__labels,
                                          self.__args)
    my_validator.validate(self.__args.valid_iters)

  def run(self):
    """ Runs the experiment. Performs the action selected by the user. """
    if self.__args.valid_dataset:
      # Perform validation.
      self.validate()
    else:
      # Otherwise, train.
      self.train()
