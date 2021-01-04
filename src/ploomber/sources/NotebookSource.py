"""

On languages and kernels
------------------------
NotebookSource represents source code in a Jupyter notebook format (language
agnostic). Apart from .ipynb, we also support any other extension supported
by jupytext.

Given a notebook, we have to know which language it is written in to extract
upstream/product variables (though this only happens when the option of
extracting dependencies automatically is on), we also have to determine the
Jupyter kernel to use (this is always needed).

The unequivocal place to store this information is in the notebook metadata
section, but given that we advocate for the use of scripts (converted to
notebooks via jupytext), they most likely won't contain metadata (metadata
saving is turned off by default in jupytext), so we have to infer this
ourselves.

To make things more complex, jupytext adds its own metadata section but we are
ignoring that for now.

Given that there are many places where this information might be stored, we
have a few rules to automatically determine language and kernel given a
script/notebook.
"""
import ast
from inspect import getargspec
from pathlib import Path
from io import StringIO
import warnings

import parso
from papermill.parameterize import parameterize_notebook
import nbformat

from ploomber.exceptions import RenderError, SourceInitializationError
from ploomber.placeholders.Placeholder import Placeholder
from ploomber.util import requires
from ploomber.sources import Source
from ploomber.static_analysis.extractors import extractor_class_for_language
from ploomber.sources import docstring


class NotebookSource(Source):
    """
    A source object representing a jupyter notebook (or any format supported
    by jupytext)

    Parameters
    ----------
    hot_reload : bool, optional
        Makes the notebook always read the file before rendering

    kernelspec_name : str, optional
        Which kernel to use for executing the notebook, it overrides any
        existing kernelspec metadata in the notebook. If the notebook does
        not have kernelspec info, this parameter is required. Defaults to None.
        To see which kernelspecs are available run "jupyter kernelspec list"

    Notes
    -----
    The render method prepares the notebook for execution: it adds the
    parameters and it makes sure kernelspec is defined
    """
    @requires([
        'parso', 'pyflakes', 'jupytext', 'nbformat', 'papermill',
        'jupyter_client'
    ])
    def __init__(self,
                 primitive,
                 hot_reload=False,
                 ext_in=None,
                 kernelspec_name=None,
                 static_analysis=False):
        # any non-py file must first be converted using jupytext, we need
        # that representation for validation, if input is already a .py file
        # do not convert. If passed a string, try to guess format using
        # jupytext. We also need ipynb representation for .develop(),
        # but do lazy loading in case we don't need both
        self._primitive = primitive

        # this happens if using SourceLoader
        if isinstance(primitive, Placeholder):
            self._path = primitive.path
            self._primitive = str(primitive)
        elif isinstance(primitive, str):
            self._path = None
            self._primitive = primitive
        elif isinstance(primitive, Path):
            self._path = primitive
            self._primitive = primitive.read_text()
        else:
            raise TypeError('Notebooks must be initialized from strings, '
                            'Placeholder or pathlib.Path, got {}'.format(
                                type(primitive)))

        self.static_analysis = static_analysis
        self._kernelspec_name = kernelspec_name
        self._hot_reload = hot_reload

        # TODO: validate ext_in values and extensions

        if self._path is None and hot_reload:
            raise ValueError('hot_reload only works in the notebook was '
                             'loaded from a file')

        if self._path is not None and ext_in is None:
            self._ext_in = self._path.suffix[1:]
        elif self._path is None and ext_in is None:
            raise ValueError('"ext_in" cannot be None if the notebook is '
                             'initialized from a string. Either pass '
                             'a pathlib.Path object with the notebook file '
                             'location or pass the source code as string '
                             'and include the "ext_in" parameter')
        elif self._path is not None and ext_in is not None:
            raise ValueError('"ext_in" must be None if notebook is '
                             'initialized from a pathlib.Path object')
        elif self._path is None and ext_in is not None:
            self._ext_in = ext_in

        # try to determine language based on extension, though this test
        # might be inconclusive if dealing with a ipynb file, though we only
        # use this to determine the appropriate jupyter kernel when
        # initializing from a string, when initializing from files, the
        # extension is used to determine the kernel
        self._language = determine_language(self._ext_in)

        self._loc = None
        self._params = None

        self._nb_str_unrendered = None
        self._nb_obj_unrendered = None
        self._nb_str_rendered = None
        self._nb_obj_rendered = None

        # this will raise an error if kernelspec_name is invalid
        self._read_nb_str_unrendered()

        self._post_init_validation(str(self._primitive))

    @property
    def primitive(self):
        if self._hot_reload:
            self._primitive = self._path.read_text()

        return self._primitive

    def render(self, params):
        """Render notebook (fill parameters using papermill)
        """
        self._params = json_serializable_params(params)
        self._render()

    def _render(self):
        # _read_nb_str_unrendered uses hot_reload, this ensures we always get
        # the latest version
        _, nb = self._read_nb_str_unrendered()

        # this is needed for parameterize_notebook to work
        for cell in nb.cells:
            if not hasattr(cell.metadata, 'tags'):
                cell.metadata['tags'] = []
        nb.metadata['papermill'] = dict()

        # NOTE: we use parameterize_notebook instead of execute_notebook
        # with the prepare_only option because the latter adds a "papermill"
        # section on each cell's metadata, which makes it too verbose when
        # using NotebookRunner.develop() when the source is script (each cell
        # will have an empty "papermill" metadata dictionary)
        nb = parameterize_notebook(nb, self._params)

        self._nb_str_rendered = nbformat.writes(nb)
        self._post_render_validation(self._params, self._nb_str_rendered)

    def _read_nb_str_unrendered(self):
        """
        Returns the notebook representation (JSON string), this is the raw
        source code passed, does not contain injected parameters.

        Adds kernelspec info if not present based on the kernelspec_name,
        this metadata is required for papermill to know which kernel to use.

        An exception is raised if we cannot determine kernel information.
        """
        # hot_reload causes to always re-evalaute the notebook representation
        if self._nb_str_unrendered is None or self._hot_reload:
            # this is the notebook node representation
            self._nb_obj_unrendered = _to_nb_obj(
                self.primitive,
                ext=self._ext_in,
                # passing the underscored version
                # because that's the only one available
                # when this is initialized
                language=self._language,
                kernelspec_name=self._kernelspec_name)

            # get the str representation. always write from nb_obj, even if
            # this was initialized with a ipynb file, nb_obj contains
            # kernelspec info
            self._nb_str_unrendered = nbformat.writes(
                self._nb_obj_unrendered, version=nbformat.NO_CONVERT)

        return self._nb_str_unrendered, self._nb_obj_unrendered

    def _post_init_validation(self, value):
        """
        Validate notebook after initialization (run pyflakes to detect
        syntax errors)
        """
        # NOTE: what happens if I pass source code with errors to parso?
        # maybe we don't need to use pyflakes after all
        # we can also use compile. can pyflakes detect things that
        # compile cannot?
        params_cell, _ = find_cell_with_tag(self._nb_obj_unrendered,
                                            'parameters')

        if params_cell is None:
            loc = ' "{}"'.format(self.loc) if self.loc else ''
            raise SourceInitializationError(
                'Notebook{} does not have a cell tagged '
                '"parameters"'.format(loc))

    def _post_render_validation(self, params, nb_str):
        """
        Validate params passed against parameters in the notebook
        """
        if self.static_analysis:
            if self.language == 'python':
                nb = self._nb_str_to_obj(nb_str)
                check_notebook(nb, params, filename=self._path or 'notebook')
            else:
                raise NotImplementedError(
                    'static_analysis is only implemented for Python notebooks'
                    ', set the option to False')

    @property
    def doc(self):
        """
        Returns notebook docstring parsed either from a triple quoted string
        in the top cell or a top markdown markdown cell
        """
        return docstring.extract_from_nb(self._nb_obj_unrendered)

    @property
    def loc(self):
        return self._path

    @property
    def name(self):
        return self._path.name

    @property
    def nb_str_rendered(self):
        """
        Returns the notebook (as a string) with parameters injected, hot
        reloadig if necessary
        """
        if self._nb_str_rendered is None:
            raise RuntimeError('Attempted to get location for an unrendered '
                               'notebook, render it first')

        if self._hot_reload:
            self._render()

        return self._nb_str_rendered

    @property
    def nb_obj_rendered(self):
        """
        Returns the notebook (as an objet) with parameters injected, hot
        reloadig if necessary
        """
        if self._nb_obj_rendered is None:
            # using self.nb_str_rendered triggers hot reload if needed
            self._nb_obj_rendered = self._nb_str_to_obj(self.nb_str_rendered)

        return self._nb_obj_rendered

    def __str__(self):
        return '\n'.join([c.source for c in self.nb_obj_rendered.cells])

    def __repr__(self):
        if self.loc is not None:
            return "{}('{}')".format(type(self).__name__, self.loc)
        else:
            return "{}(loaded from string)".format(type(self).__name__)

    @property
    def variables(self):
        raise NotImplementedError

    @property
    def extension(self):
        # this can be Python, R, Julia, etc. We are handling them the same,
        # for now, no normalization can be done.
        # One approach is to use the ext if loaded from file, otherwise None
        return None

    # FIXME: add this to the abstract class, probably get rid of "extension"
    # since it's not informative (ipynb files can be Python, R, etc)
    @property
    def language(self):
        """
        Notebook Language (Python, R, etc), this is a best-effort property,
        can be None if we could not determine the language
        """
        if self._language is None:
            self._read_nb_str_unrendered()

            try:
                # make sure you return "r" instead of "R"
                return (self._nb_obj_unrendered.metadata.kernelspec.language.
                        lower())
            except AttributeError:
                return None

        else:
            return self._language

    def _nb_str_to_obj(self, nb_str):
        return nbformat.reads(nb_str, as_version=nbformat.NO_CONVERT)

    def _get_parameters_cell(self):
        self._read_nb_str_unrendered()
        cell, _ = find_cell_with_tag(self._nb_obj_unrendered, tag='parameters')
        return cell.source

    def extract_upstream(self):
        extractor_class = extractor_class_for_language(self.language)
        return extractor_class(self._get_parameters_cell()).extract_upstream()

    def extract_product(self):
        extractor_class = extractor_class_for_language(self.language)
        return extractor_class(self._get_parameters_cell()).extract_product()


# FIXME: some of this only applies to Python notebooks (error about missing
# parameters cells applies to every notebook), make sure the source takes
# this into account, also check if there are any other functions that
# are python specific
def check_notebook(nb, params, filename):
    """
    Perform static analysis on a Jupyter notebook code cell sources

    Parameters
    ----------
    nb_source : str
        Jupyter notebook source code in jupytext's py format,
        must have a cell with the tag "parameters"

    params : dict
        Parameter that will be added to the notebook source

    filename : str
        Filename to identify pyflakes warnings and errors

    Raises
    ------
    RenderError
        If the notebook does not have a cell with the tag 'parameters',
        if the parameters in the notebook do not match the passed params or
        if pyflakes validation fails
    """
    # variable to collect all error messages
    error_message = '\n'

    params_cell, _ = find_cell_with_tag(nb, 'parameters')

    # compare passed parameters with declared
    # parameters. This will make our notebook behave more
    # like a "function", if any parameter is passed but not
    # declared, this will return an error message, if any parameter
    # is declared but not passed, a warning is shown
    res_params = compare_params(params_cell['source'], params)
    error_message += res_params

    # run pyflakes and collect errors
    res = check_source(nb, filename=filename)

    # pyflakes returns "warnings" and "errors", collect them separately
    if res['warnings']:
        error_message += 'pyflakes warnings:\n' + res['warnings']

    if res['errors']:
        error_message += 'pyflakes errors:\n' + res['errors']

    # if any errors were returned, raise an exception
    if error_message != '\n':
        raise RenderError(error_message)

    return True


def json_serializable_params(params):
    # papermill only allows JSON serializable parameters
    # convert Params object to dict
    params = params.to_dict()
    params['product'] = params['product'].to_json_serializable()

    if params.get('upstream'):
        params['upstream'] = {
            k: n.to_json_serializable()
            for k, n in params['upstream'].items()
        }
    return params


def compare_params(params_source, params):
    """
    Compare the parameters cell's source with the passed parameters, warn
    on missing parameter and raise error if an extra parameter was passed.
    """
    # params are keys in "params" dictionary
    params = set(params)

    # use parso to parse the "parameters" cell source code and get all
    # variable names declared
    declared = set(parso.parse(params_source).get_used_names().keys())

    # now act depending on missing variables and/or extra variables

    missing = declared - params
    extra = params - declared

    if missing:
        warnings.warn(
            'Missing parameters: {}, will use default value'.format(missing))

    if extra:
        return 'Passed non-declared parameters: {}'.format(extra)
    else:
        return ''


def check_source(nb, filename):
    """
    Run pyflakes on a notebook, wil catch errors such as missing passed
    parameters that do not have default values
    """
    from pyflakes.api import check as pyflakes_check
    from pyflakes.reporter import Reporter

    # concatenate all cell's source code in a single string
    source = '\n'.join([c['source'] for c in nb.cells])

    # this objects are needed to capture pyflakes output
    warn = StringIO()
    err = StringIO()
    reporter = Reporter(warn, err)

    # run pyflakes.api.check on the source code
    pyflakes_check(source, filename=filename, reporter=reporter)

    warn.seek(0)
    err.seek(0)

    # return any error messages returned by pyflakes
    return {
        'warnings': '\n'.join(warn.readlines()),
        'errors': '\n'.join(err.readlines())
    }


def _to_nb_obj(source, language, ext=None, kernelspec_name=None):
    """
    Convert to jupyter notebook via jupytext, if the notebook does not contain
    kernel information and the user did not pass a kernelspec_name explicitly,
    we will try to infer the language and select a kernel appropriately.

    If a valid kernel is found, it is added to the notebook. If none of this
    works, an exception is raised.

    If also converts the code string to its notebook node representation,
    adding kernel data accordingly.

    Parameters
    ----------
    source : str
        Jupyter notebook (or jupytext compatible formatted) document

    language : str
        Programming language

    Returns
    -------
    nb
        Notebook object


    Raises
    ------
    RenderError
        If the notebook has no kernelspec metadata and kernelspec_name is
        None. A notebook without kernelspec metadata will not display in
        jupyter notebook correctly. We have to make sure all notebooks
        have this.
    """
    import jupytext

    # let jupytext figure out the format
    nb = jupytext.reads(source, fmt=ext)

    ensure_kernelspec(nb, kernelspec_name, ext, language)

    return nb


def ensure_kernelspec(nb, kernelspec_name, ext, language):
    """Make sure the passed notebook has kernel info
    """
    import jupyter_client

    kernel_name = determine_kernel_name(nb, kernelspec_name, ext, language)

    # cannot keep going if we don't have the kernel name
    if kernel_name is None:
        raise SourceInitializationError(
            'Notebook does not contain kernelspec metadata and '
            'kernelspec_name was not specified, either add '
            'kernelspec info to your source file or specify '
            'a kernelspec by name. To see list of installed kernels run '
            '"jupyter kernelspec list" in the terminal (first column '
            'indicates the name). Python is usually named "python3", '
            'R usually "ir"')

    kernelspec = jupyter_client.kernelspec.get_kernel_spec(kernel_name)

    nb.metadata.kernelspec = {
        "display_name": kernelspec.display_name,
        "language": kernelspec.language,
        "name": kernel_name
    }


def determine_kernel_name(nb, kernelspec_name, ext, language):
    """
    Determines the kernel name by using the following data (returns whatever
    gives kernel info first): 1) explicit kernel from the user 2) notebook's
    metadata 3) file extension 4) language 5) best guess
    """
    # explicit kernelspec name
    if kernelspec_name is not None:
        return kernelspec_name

    # use metadata info
    try:
        return nb.metadata.kernelspec.name
    except AttributeError:
        pass

    # use language from extension if passed, otherwise use language variable
    if ext:
        language = determine_language(ext)

    lang2kernel = {'python': 'python3', 'r': 'ir'}

    if language in lang2kernel:
        return lang2kernel[language]

    # nothing worked, try to guess if it's python...
    is_python_ = is_python(nb)

    if is_python_:
        return 'python3'
    else:
        return None


def inject_cell(model, params):
    """Inject params (by adding a new cell) to a model

    Notes
    -----
    A model is different than a notebook:
    https://jupyter-notebook.readthedocs.io/en/stable/extending/contents.html
    """
    nb = nbformat.from_dict(model['content'])

    # we must ensure nb has kernelspec info, otherwise papermill will fail to
    # parametrize
    ext = model['name'].split('.')[-1]
    ensure_kernelspec(nb, kernelspec_name=None, ext=ext, language=None)

    # papermill adds a bunch of things before calling parameterize_notebook
    # if we don't add those things, parameterize_notebook breaks
    # https://github.com/nteract/papermill/blob/0532d499e13e93d8990211be33e9593f1bffbe6c/papermill/iorw.py#L400
    if not hasattr(nb.metadata, 'papermill'):
        nb.metadata['papermill'] = {
            'parameters': dict(),
            'environment_variables': dict(),
            'version': None,
        }

    for cell in nb.cells:
        if not hasattr(cell.metadata, 'tags'):
            cell.metadata['tags'] = []

    params = json_serializable_params(params)

    comment = ('This cell was injected automatically based on your stated '
               'upstream dependencies (cell above) and pipeline.yaml '
               'preferences. It is temporary and will be removed when you '
               'save this notebook')

    model['content'] = parameterize_notebook(nb,
                                             params,
                                             report_mode=False,
                                             comment=comment)


# FIXME: this is used in the task itself in the .develop() feature, maybe
# move there?
def _cleanup_rendered_nb(nb):
    cell, i = find_cell_with_tag(nb, 'injected-parameters')

    if i is not None:
        print('Removing injected-parameters cell...')
        nb['cells'].pop(i)

    cell, i = find_cell_with_tag(nb, 'debugging-settings')

    if i is not None:
        print('Removing debugging-settings cell...')
        nb['cells'].pop(i)

    # papermill adds "tags" to all cells that don't have them, remove them
    # if they are empty to avoid cluttering the script
    for cell in nb['cells']:
        if 'tags' in cell.get('metadata', {}):
            if not len(cell['metadata']['tags']):
                del cell['metadata']['tags']

    return nb


def is_python(nb):
    """
    Determine if the notebook is Python code for a given notebook object, look
    for metadata.kernelspec.language first, if not defined, try to guess if
    it's Python, it's conservative and it returns False if the code is valid
    Python but contains (<-), in which case it's much more likely to be R
    """
    is_python_ = None

    # check metadata first
    try:
        language = nb.metadata.kernelspec.language
    except AttributeError:
        pass
    else:
        is_python_ = language == 'python'

    # no language defined in metadata, check if it's valid python
    if is_python_ is None:
        code_str = '\n'.join([c.source for c in nb.cells])

        try:
            ast.parse(code_str)
        except SyntaxError:
            is_python_ = False
        else:
            # there is a lot of R code which is also valid Python code! So
            # let's
            # run a quick test. It is very unlikely to have "<-" in Python (
            # {less than} {negative} but extremely common {assignment}
            if '<-' not in code_str:
                is_python_ = True

    # inconclusive test...
    if is_python_ is None:
        is_python_ = False

    return is_python_


def find_cell_with_tag(nb, tag):
    """
    Find a cell with a given tag, returns a cell, index tuple. Otherwise
    (None, None)
    """
    for i, c in enumerate(nb['cells']):
        cell_tags = c['metadata'].get('tags')
        if cell_tags:
            if tag in cell_tags:
                return c, i

    return None, None


def determine_language(extension):
    """
    A function to determine programming language given file extension,
    returns programming language name (all lowercase) if could be determined,
    None if the test is inconclusive
    """
    if extension.startswith('.'):
        extension = extension[1:]

    mapping = {'py': 'python', 'r': 'r', 'R': 'r', 'Rmd': 'r', 'rmd': 'r'}

    # ipynb can be many languages, it must return None
    return mapping.get(extension)
