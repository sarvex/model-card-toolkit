# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Model Card Toolkit.

The Model Card Toolkit (MCT) provides a set of utilities to generate Model Cards
from trained models, evaluations, and datasets in ML pipelines.
"""

import dataclasses
import logging
import os
import pkgutil
import tempfile
from typing import List, Optional, Text, Union

from absl import logging
import jinja2

from model_card_toolkit.model_card import ModelCard
from model_card_toolkit.proto import model_card_pb2
from model_card_toolkit.utils import graphics
from model_card_toolkit.utils import tfx_util
import tensorflow_model_analysis as tfma

import ml_metadata as mlmd

# Constants about provided UI templates.
_UI_TEMPLATES = (
    'template/html/default_template.html.jinja',
    'template/md/default_template.md.jinja',
)
_DEFAULT_UI_TEMPLATE_FILE = os.path.join('html', 'default_template.html.jinja')

# Constants about Model Cards Toolkit Assets (MCTA).
_MCTA_PROTO_FILE = os.path.join('data', 'model_card.proto')
_MCTA_TEMPLATE_DIR = 'template'
_MCTA_RESOURCE_DIR = os.path.join('resources', 'plots')

# Constants about the final generated model cards.
_MODEL_CARDS_DIR = 'model_cards'
_DEFAULT_MODEL_CARD_FILE_NAME = 'model_card.html'


@dataclasses.dataclass
class Source:
  """Sources to extract data for a model card.

  Attributes:
    eval_result_paths: The paths to the output from TensorFlow Model Analysis or
      TFX Evaluator.
    eval_result_file_format: Optional file extension to filter eval result files
      by.
    dataset_statistics_paths: The paths to the output from TensorFlow Data
      Validation or TFX ExampleValidator.
  """
  eval_result_paths: List[Text] = dataclasses.field(default_factory=list)
  eval_result_file_format: Optional[Text] = ''
  dataset_statistics_paths: List[Text] = dataclasses.field(default_factory=list)


class ModelCardToolkit():
  """ModelCardToolkit provides utilities to generate a ModelCard.

  ModelCardToolkit is a tool for ML practitioners to create Model Cards,
  documentation for model information such as owners, use cases, training and
  evaluation data, performance, etc. A Model Card document can be displayed in
  output formats including HTML, Markdown, etc.

  The ModelCardToolkit includes an API designed for a human-in-the-loop process
  to elaborate the ModelCard. If model training is integrated with ML Metadata
  (e.g., TFX pipelines), the ModelCardToolkit can further populate ModelCard
  fields by extract metadata and lineage from the model's MLMD instance.

  The ModelCardToolkit organizes the ModelCard assets (e.g., structured data,
  plots, and UI templates) in a user-specified directory, and updates them
  incrementally via its API.


  Example usage:

  ```python
  import model_card_toolkit

  # Initialize the Model Card Toolkit with a path to store generate assets
  model_card_output_path = ...
  mct = model_card_toolkit.ModelCardToolkit(model_card_output_path)

  # Initialize the ModelCard, which can be freely populated
  model_card = mct.scaffold_assets()
  model_card.model_details.name = 'My Model'

  # Write the model card data to a proto file
  mct.update_model_card(model_card)

  # Return the model card document as an HTML page
  html = mct.export_format()
  ```
  """

  # TODO(b/188707257): combine mlmd_store and model_uri args
  def __init__(self,
               output_dir: Optional[Text] = None,
               mlmd_store: Optional[mlmd.MetadataStore] = None,
               model_uri: Optional[Text] = None,
               source: Optional[Source] = None):
    """Initializes the ModelCardToolkit.

    This function does not generate any assets by itself. Use the other API
    functions to generate Model Card assets. See class-level documentation for
    example usage.

    Args:
      output_dir: The path where MCT assets (such as data files and model cards)
        are written to. If not provided, a temp directory is used.
      mlmd_store: A ml-metadata MetadataStore to retrieve metadata and lineage
        information about the model stored at `model_uri`. If given, a set of
        model card properties can be auto-populated from the `mlmd_store`.
      model_uri: The path to the trained model to generate model cards. Ignored
        if mlmd_store is not used.
      source: A collection of sources to extract data for a model card. This can
        be used instead of `mlmd_store`, or alongside it. Useful when using
        tools like TensorFlow Model Analysis and Data Validation without writing
        to a MLMD store.

    Raises:
      ValueError: If `mlmd_store` is given and the `model_uri` cannot be
        resolved as a model artifact in the metadata store.
    """
    self.output_dir = output_dir or tempfile.mkdtemp()
    self._mcta_proto_file = os.path.join(self.output_dir, _MCTA_PROTO_FILE)
    self._mcta_template_dir = os.path.join(self.output_dir, _MCTA_TEMPLATE_DIR)
    self._model_cards_dir = os.path.join(self.output_dir, _MODEL_CARDS_DIR)
    self._source = source

    # if mlmd_store and model_uri are both set, use them
    self._store = mlmd_store
    if mlmd_store and model_uri:
      models = self._store.get_artifacts_by_uri(model_uri)
      if not models:
        raise ValueError(f'"{model_uri}" cannot be found in the `mlmd_store`.')
      if len(models) > 1:
        logging.info(
            '%d artifacts are found with the `model_uri`="%s". '
            'The last one is used.', len(models), model_uri)
      self._artifact_with_model_uri = models[-1]
    elif mlmd_store:
      raise ValueError('If `mlmd_store` is set, `model_uri` should be set.')
    elif model_uri:
      logging.info('`model_uri` ignored when `mlmd_store` is not set.')

  def _jinja_loader(self, template_dir: Text):
    return jinja2.FileSystemLoader(template_dir)

  def _write_file(self, path: Text, content: Text) -> None:
    """Write content to the path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w+') as f:
      f.write(content)

  def _write_proto_file(
      self, path: Text, model_card: Union[ModelCard,
                                          model_card_pb2.ModelCard]) -> None:
    """Write serialized model card proto to the path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
      if isinstance(model_card, ModelCard):
        f.write(model_card.to_proto().SerializeToString())
      else:
        f.write(model_card.SerializeToString())

  def _read_proto_file(self, path: Text) -> ModelCard:
    """Read serialized model card proto from the path."""
    model_card_proto = model_card_pb2.ModelCard()
    with open(path, 'rb') as f:
      model_card_proto.ParseFromString(f.read())
    return ModelCard().copy_from_proto(model_card_proto)

  def _scaffold_model_card(self) -> ModelCard:
    """Generates the ModelCard for scaffold_assets().

    If Source is provided, pre-populate ModelCard fields with data from Source.
    If MLMD store is provided, pre-populate ModelCard fields with data from
    MLMD. See `model_card_toolkit.utils.tfx_util` and
    `model_card_toolkit.utils.graphics` documentation for more details.

    Returns:
      A ModelCard representing the given model.
    """
    # Pre-populate ModelCard fields
    if self._store:
      model_card = tfx_util.generate_model_card_for_model(
          self._store, self._artifact_with_model_uri.id)
    else:
      model_card = ModelCard()

    # Generate graphics for TFMA's `EvalResult`s
    if self._source and self._source.eval_result_paths:
      for eval_result_path in self._source.eval_result_paths:
        if eval_result := tfma.load_eval_result(
            output_path=eval_result_path,
            output_file_format=self._source.eval_result_file_format,
        ):
          logging.info('EvalResult found at path %s', eval_result_path)
          tfx_util.annotate_eval_result_metrics(model_card, eval_result)
          graphics.annotate_eval_result_plots(model_card, eval_result)
        else:
          logging.info('EvalResult not found at path %s', eval_result_path)
    if self._store:
      metrics_artifacts = tfx_util.get_metrics_artifacts_for_model(
          self._store, self._artifact_with_model_uri.id)
      for metrics_artifact in metrics_artifacts:
        eval_result = tfx_util.read_metrics_eval_result(metrics_artifact.uri)
        if eval_result is not None:
          tfx_util.annotate_eval_result_metrics(model_card, eval_result)
          graphics.annotate_eval_result_plots(model_card, eval_result)

    # Generate graphics for TFDV's `DatasetFeatureStatisticsList`s
    if self._source and self._source.dataset_statistics_paths:
      for dataset_statistics_path in self._source.dataset_statistics_paths:
        data_stats = tfx_util.read_stats_protos(dataset_statistics_path)
        graphics.annotate_dataset_feature_statistics_plots(
            model_card, data_stats)
    if self._store:
      stats_artifacts = tfx_util.get_stats_artifacts_for_model(
          self._store, self._artifact_with_model_uri.id)
      for stats_artifact in stats_artifacts:
        data_stats = tfx_util.read_stats_protos(stats_artifact.uri)
        graphics.annotate_dataset_feature_statistics_plots(
            model_card, data_stats)

    return model_card

  def scaffold_assets(self) -> ModelCard:
    """Generates the Model Card Tookit assets.

    Assets include the ModelCard proto file, Model Card document, and jinja
    template. These are written to the `output_dir` declared at
    initialization.

    An assets directory is created if one does not already exist.

    If the MCT is initialized with a `mlmd_store`, it further auto-populates
    the model cards properties as well as generating related plots such as model
    performance and data distributions.

    Returns:
      A ModelCard representing the given model.

    Raises:
      FileNotFoundError: on failure to copy the template files.
    """

    # Generate ModelCard.
    model_card = self._scaffold_model_card()

    # Write Proto file.
    self._write_proto_file(self._mcta_proto_file, model_card)

    # Write UI template files.
    for template_path in _UI_TEMPLATES:
      template_content = pkgutil.get_data('model_card_toolkit', template_path)
      if template_content is None:
        raise FileNotFoundError(f"Cannot find file: '{template_path}'")
      template_content = template_content.decode('utf8')
      self._write_file(
          os.path.join(self.output_dir, template_path), template_content)

    return model_card

  def update_model_card(
      self, model_card: Union[ModelCard, model_card_pb2.ModelCard]) -> None:
    """Updates the Proto file in the MCT assets directory.

    Args:
      model_card: The updated model card to write back.

    Raises:
       Error: when the given model_card is invalid w.r.t. the schema.
    """
    self._write_proto_file(self._mcta_proto_file, model_card)

  def export_format(self,
                    model_card: Optional[Union[
                        ModelCard, model_card_pb2.ModelCard]] = None,
                    template_path: Optional[Text] = None,
                    output_file=_DEFAULT_MODEL_CARD_FILE_NAME) -> Text:
    """Generates a model card document based on the MCT assets.

    The model card document is both returned by this function, as well as saved
    to output_file.

    Args:
      model_card: The ModelCard object, generated from `scaffold_assets()`. If
        not provided, it will be read from the ModelCard proto file in the
        assets directory.
      template_path: The file path of the Jinja template. If not provided, the
        default template will be used.
      output_file: The file name of the generated model card. If not provided,
        the default 'model_card.html' will be used. If the file already exists,
        then it will be overwritten.

    Returns:
      The model card file content.

    Raises:
      MCTError: If `export_format` is called before `scaffold_assets` has
        generated model card assets.
    """
    if not template_path:
      template_path = os.path.join(self._mcta_template_dir,
                                   _DEFAULT_UI_TEMPLATE_FILE)
    template_dir = os.path.dirname(template_path)
    template_file = os.path.basename(template_path)

    # If model_card is passed in, write to Proto file.
    if model_card:
      self.update_model_card(model_card)
    # If model_card is not passed in, read from Proto file.
    elif os.path.exists(self._mcta_proto_file):
      model_card = self._read_proto_file(self._mcta_proto_file)
    # If model card proto never created, raise exception.
    else:
      raise ValueError(
          'scaffold_assets() must be called before export_format().')

    # Generate Model Card.
    jinja_env = jinja2.Environment(
        loader=self._jinja_loader(template_dir),
        autoescape=True,
        auto_reload=True,
        cache_size=0)
    template = jinja_env.get_template(template_file)
    model_card_file_content = template.render(
        model_details=model_card.model_details,
        model_parameters=model_card.model_parameters,
        quantitative_analysis=model_card.quantitative_analysis,
        considerations=model_card.considerations)

    # Write the model card document file and return its contents.
    mode_card_file_path = os.path.join(self._model_cards_dir, output_file)
    self._write_file(mode_card_file_path, model_card_file_content)
    return model_card_file_content

  def save_mlmd(self) -> None:
    """Saves the model card of the model artifact with `model_uri` to MLMD."""
    pass
