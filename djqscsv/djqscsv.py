import datetime

import unicodecsv as csv

from django.core.exceptions import ValidationError
from django.utils.text import slugify
from django.http import HttpResponse

from django.utils import six

import itertools

""" A simple python package for turning django models into csvs """

# Keyword arguments that will be used by this module
# the rest will be passed along to the csv writer
DJQSCSV_KWARGS = {'field_header_map': None,
                  'field_serializer_map': None,
                  'use_verbose_names': True,
                  'field_order': None,
                  'flattern_fields': None}


class CSVException(Exception):
    pass


def render_to_csv_response(queryset, filename=None, append_datestamp=False,
                           **kwargs):
    """
    provides the boilerplate for making a CSV http response.
    takes a filename or generates one from the queryset's model.
    """
    if filename:
        filename = _validate_and_clean_filename(filename)
        if append_datestamp:
            filename = _append_datestamp(filename)
    else:
        filename = generate_filename(queryset,
                                     append_datestamp=append_datestamp)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename=%s;' % filename
    response['Cache-Control'] = 'no-cache'

    write_csv(queryset, response, **kwargs)

    return response

"""
When you call values() on a queryset where the Model has a ManyToManyField
and there are multiple related items, it returns a separate dictionary for each
related item. This function merges the dictionaries so that there is only
one dictionary per id at the end, with lists of related items for each.
"""
def merge_values(values, merge_key):
    grouped_results = itertools.groupby(values, key=lambda value: value[merge_key])

    merged_values = []
    for k, g in grouped_results:
        groups = list(g)
        merged_value = {}
        for group in groups:
            for key, val in group.iteritems():
                if not merged_value.get(key):
                    merged_value[key] = val
                elif val != merged_value[key]:
                    if isinstance(merged_value[key], list):
                        if val not in merged_value[key]:
                            merged_value[key].append(val)
                    else:
                        old_val = merged_value[key]
                        merged_value[key] = [old_val, val]
        merged_values.append(merged_value)
    return merged_values

def write_csv(queryset, file_obj, **kwargs):
    """
    The main worker function. Writes CSV data to a file object based on the
    contents of the queryset.
    """

    # process keyword arguments to pull out the ones used by this function
    field_header_map = kwargs.get('field_header_map', {})
    field_serializer_map = kwargs.get('field_serializer_map', {})
    use_verbose_names = kwargs.get('use_verbose_names', True)
    field_order = kwargs.get('field_order', None)
    flattern_fields = kwargs.get('flattern_fields', [])

    csv_kwargs = {'encoding': 'utf-8'}

    for key, val in six.iteritems(kwargs):
        if key not in DJQSCSV_KWARGS:
            csv_kwargs[key] = val

    # add BOM to support CSVs in MS Excel (for Windows only)
    file_obj.write(b'\xef\xbb\xbf')

    # the CSV must always be built from a values queryset
    # in order to introspect the necessary fields.
    # However, repeated calls to values can expose fields that were not
    # present in the original qs. If using `values` as a way to
    # scope field permissions, this is unacceptable. The solution
    # is to make sure values is called *once*.

    # perform an string check to avoid a non-existent class in certain
    # versions
    if type(queryset).__name__ == 'ValuesQuerySet':
        values_qs = queryset
    else:
        # could be a non-values qs, or could be django 1.9+
        iterable_class = getattr(queryset, '_iterable_class', object)
        if iterable_class.__name__ == 'ValuesIterable':
            values_qs = queryset
        else:
            values_qs = queryset.values()

    try:
        field_names = values_qs.query.values_select
    except AttributeError:
        try:
            field_names = values_qs.field_names
        except AttributeError:
            # in django1.5, empty querysets trigger
            # this exception, but not django 1.6
            raise CSVException("Empty queryset provided to exporter.")

    extra_columns = list(values_qs.query.extra_select)
    if extra_columns:
        field_names += extra_columns

    try:
        aggregate_columns = list(values_qs.query.annotation_select)
    except AttributeError:
        # this gets a deprecation warning in django 1.9 but is
        # required in django<=1.7
        aggregate_columns = list(values_qs.query.aggregate_select)

    if aggregate_columns:
        field_names += aggregate_columns

    if field_order:
        # go through the field_names and put the ones
        # that appear in the ordering list first
        field_names = ([field for field in field_order
                       if field in field_names] +
                       [field for field in field_names
                        if field not in field_order])

    writer = csv.DictWriter(file_obj, field_names, **csv_kwargs)

    # verbose_name defaults to the raw field name, so in either case
    # this will produce a complete mapping of field names to column names
    name_map = dict((field, field) for field in field_names)
    if use_verbose_names:
        name_map.update(
            dict((field.name, field.verbose_name)
                 for field in queryset.model._meta.fields
                 if field.name in field_names))

    # merge the custom field headers into the verbose/raw defaults, if provided
    merged_header_map = name_map.copy()
    if extra_columns:
        merged_header_map.update(dict((k, k) for k in extra_columns))
    merged_header_map.update(field_header_map)

    writer.writerow(merged_header_map)

    unique_field = None
    discard_unique_field = False
    if flattern_fields:
        if values_qs.model._meta.pk.name in merged_header_map.keys():
            unique_field = values_qs.model._meta.pk.name
        elif 'pk' in merged_header_map.keys():
            unique_field = 'pk'
        else:
            values_qs = values_qs.values(*(values_qs.query.values_select + [values_qs.model._meta.pk.name]))
            unique_field = values_qs.model._meta.pk.name
            discard_unique_field = True
        values = merge_values(values_qs, unique_field)
    else:
        values = values_qs

    for record in values:
        if discard_unique_field:
            record.pop(unique_field)
        record = _sanitize_record(field_serializer_map, record)
        writer.writerow(record)


def generate_filename(queryset, append_datestamp=False):
    """
    Takes a queryset and returns a default
    base filename based on the underlying model
    """
    base_filename = slugify(six.text_type(queryset.model.__name__)) \
        + '_export.csv'

    if append_datestamp:
        base_filename = _append_datestamp(base_filename)

    return base_filename

########################################
# utility functions
########################################


def _validate_and_clean_filename(filename):

    if filename.count('.'):
        if not filename.endswith('.csv'):
            raise ValidationError('the only accepted file extension is .csv')
        else:
            filename = filename[:-4]

    filename = slugify(six.text_type(filename)) + '.csv'
    return filename


def _sanitize_record(field_serializer_map, record):

    def _serialize_value(value):
        # provide default serializer for the case when
        # non text values get sent without a serializer
        if isinstance(value, datetime.datetime):
            return value.isoformat()
        else:
            return six.text_type(value)

    obj = {}
    for key, val in six.iteritems(record):
        if val is not None:
            serializer = field_serializer_map.get(key, _serialize_value)
            newval = serializer(val)
            # If the user provided serializer did not produce a string,
            # coerce it to a string
            if not isinstance(newval, six.text_type):
                newval = six.text_type(newval)
            obj[key] = newval

    return obj


def _append_datestamp(filename):
    """
    takes a filename and returns a new filename with the
    current formatted date appended to it.

    raises an exception if it receives an unclean filename.
    validation/preprocessing must be called separately.
    """
    if filename != _validate_and_clean_filename(filename):
        raise ValidationError('cannot datestamp unvalidated filename')

    formatted_datestring = datetime.date.today().strftime("%Y%m%d")
    return '%s_%s.csv' % (filename[:-4], formatted_datestring)
