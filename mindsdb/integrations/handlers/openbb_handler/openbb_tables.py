from mindsdb.integrations.libs.api_handler import APITable
from mindsdb_sql.parser import ast
from mindsdb.integrations.utilities.date_utils import parse_local_date
from mindsdb.integrations.utilities.sql_utils import extract_comparison_conditions, project_dataframe, filter_dataframe
from mindsdb.integrations.utilities.sql_utils import sort_dataframe
from mindsdb.utilities import log

from typing import Dict, List, Union
from pydantic import ValidationError

import duckdb
import pandas as pd

logger = log.getLogger(__name__)


def create_table_class(
    params_metadata,
    response_metadata,
    obb_function,
    func_docs="",
    provider=None
):
    """Creates a table class for the given OpenBB Platform function."""
    mandatory_fields = [key for key in params_metadata['fields'].keys() if params_metadata['fields'][key].is_required() is True]
    response_columns = list(response_metadata['fields'].keys())

    class AnyTable(APITable):
        def _get_params_from_conditions(self, conditions: List) -> Dict:
            """Gets aggregate trade data API params from SQL WHERE conditions.

            Returns params to use for Binance API call to klines.

            Args:
                conditions (List): List of individual SQL WHERE conditions.
            """
            params: dict = {}
            # generic interpreter for conditions
            # since these are all equality conditions due to OpenBB Platform's API
            # then we can just use the first arg as the key and the second as the value
            for op, arg1, arg2 in conditions:
                if op == "=":
                    params[arg1] = arg2

            return params

        def _select_dataframe(self, query: ast.Select, df: pd.DataFrame) -> pd.DataFrame:
            """Selects data from a DataFrame using DuckDB"""
            df = df
            openbb_table = query.from_table.parts[-1]
            query.from_table = ast.Identifier('df')
            query_str = query.to_string()
            # Need to get rid of references to the OpenBB table (e.g. crypto_price_historical.close -> close)
            query_str = query_str.replace(f'{openbb_table}.', '')
            # Get rid of `` wrapping identifiers
            query_str = query_str.replace('`', '')
            duckdb_query_result = duckdb.query(query_str)
            result = duckdb_query_result.to_df()
            return result

        def select(self, query: ast.Select) -> pd.DataFrame:
            """Selects data from the OpenBB Platform and returns it as a pandas DataFrame.

            Returns dataframe representing the OpenBB data.

            Args:
                query (ast.Select): Given SQL SELECT query
            """
            conditions = extract_comparison_conditions(query.where)
            arg_params = self._get_params_from_conditions(conditions=conditions)

            params = {}
            if provider is not None:
                params['provider'] = provider

            filters = []
            mandatory_args_set = {key: False for key in mandatory_fields}
            columns_to_add = {}
            strict_filter = arg_params.get('strict_filter', False)

            for op, arg1, arg2 in conditions:
                if op == 'or':
                    raise NotImplementedError('OR is not supported')

                if arg1 in mandatory_fields:
                    mandatory_args_set[arg1] = True

                if ('start_' + arg1 in params_metadata['fields'] and arg1 in response_columns and arg2 is not None):

                    if response_metadata['fields'][arg1].annotation == 'datetime':
                        date = parse_local_date(arg2)
                        interval = arg_params.get('interval', '1d')

                        if op == '>':
                            params['start_' + arg1] = date.strftime('%Y-%m-%d')
                        elif op == '<':
                            params['end_' + arg1] = date.strftime('%Y-%m-%d')
                        elif op == '>=':
                            date = date - pd.Timedelta(interval)
                            params['start_' + arg1] = date.strftime('%Y-%m-%d')
                        elif op == '<=':
                            date = date + pd.Timedelta(interval)
                            params['end_' + arg1] = date.strftime('%Y-%m-%d')
                        elif op == '=':
                            date = date - pd.Timedelta(interval)
                            params['start_' + arg1] = date.strftime('%Y-%m-%d')
                            date = date + pd.Timedelta(interval)
                            params['end_' + arg1] = date.strftime('%Y-%m-%d')

                elif arg1 in params_metadata['fields'] or not strict_filter:
                    if op == '=':
                        params[arg1] = arg2
                        columns_to_add[arg1] = arg2

                filters.append([op, arg1, arg2])

            if not all(mandatory_args_set.values()):
                missing_args = ", ".join([k for k, v in mandatory_args_set.items() if v is False])
                text = f"You must specify the following arguments in the WHERE statement: {missing_args}\n"

                # Create docstring for the current function
                text += "\nDocstring:"
                for param in params_metadata['fields']:
                    field = params_metadata['fields'][param]
                    if getattr(field.annotation, '__origin__', None) is Union:
                        annotation = f"Union[{', '.join(arg.__name__ for arg in field.annotation.__args__)}]"
                    else:
                        annotation = field.annotation.__name__
                    text += f"\n  * {param}{'' if field.is_required() else ' (optional)'}: {annotation}\n{field.description}"

                text += f"\n\nFor more information check {func_docs}"

                raise NotImplementedError(text)

            try:
                # Handle limit keyword correctly since it can't be parsed as a WHERE arg (i.e. WHERE limit = 50)
                if query.limit is not None and 'limit' in params_metadata['fields']:
                    params['limit'] = query.limit.value
                obbject = obb_function(**params)

                # Extract data in dataframe format
                result = obbject.to_df()

                if result is None:
                    raise Exception(f"For more information check {func_docs}.")

                # Check if index is a datetime, if it is we want that as a column
                if isinstance(result.index, pd.DatetimeIndex):
                    result.reset_index(inplace=True)

                if query.order_by:
                    result = sort_dataframe(result, query.order_by)

                if query.limit is not None:
                    result = result.head(query.limit.value)

                    if result is None:
                        raise Exception(f"For more information check {func_docs}.")

                for key in columns_to_add:
                    result[key] = params[key]

                # filter targets
                result = filter_dataframe(result, filters)

                if result is None:
                    raise Exception(f"For more information check {func_docs}.")

                columns = self.get_columns()

                columns += [col for col in result.columns if col not in columns]

                for full_target in query.targets:
                    if isinstance(full_target, ast.Star):
                        continue
                    if isinstance(full_target, ast.Identifier):
                        target = full_target.parts[-1].lower()
                    elif isinstance(full_target, ast.Function):
                        target = full_target.args[0].parts[-1].lower()
                    else:
                        # Could be a window function or other operation we can't handle. Defer to DuckDB.
                        return self._select_dataframe(query, result)
                    if target not in columns:
                        raise ValueError(f"Unknown column '{target}' in 'field list'")

                # project targets
                try:
                    result = project_dataframe(result, query.targets, columns)
                except NotImplementedError:
                    # Target contains a function that we need DuckDB to resolve.
                    return self._select_dataframe(query, result)
                return result

            except AttributeError as e:
                logger.info(f'Encountered error while executing OpenBB select: {str(e)}')

                # Create docstring for the current function
                text = "Docstring:"
                for param in params_metadata['fields']:
                    field = params_metadata['fields'][param]
                    if getattr(field.annotation, '__origin__', None) is Union:
                        annotation = f"Union[{', '.join(arg.__name__ for arg in field.annotation.__args__)}]"
                    else:
                        annotation = field.annotation.__name__
                    text += f"\n  * {param}{'' if field.is_required() else ' (optional)'}: {annotation}\n{field.description}"

                text += f"\n\nFor more information check {func_docs}"

                raise Exception(f"{str(e)}\n\n{text}.") from e

            except ValidationError as e:
                logger.info(f'Encountered error while executing OpenBB select: {str(e)}')

                # Create docstring for the current function
                text = "Docstring:"
                for param in params_metadata['fields']:
                    field = params_metadata['fields'][param]
                    if getattr(field.annotation, '__origin__', None) is Union:
                        annotation = f"Union[{', '.join(arg.__name__ for arg in field.annotation.__args__)}]"
                    else:
                        annotation = field.annotation.__name__
                    text += f"\n  * {param}{'' if field.is_required() else ' (optional)'}: {annotation}\n{field.description}"

                text += f"\n\nFor more information check {func_docs}"

                raise Exception(f"{str(e)}\n\n{text}.") from e

            except Exception as e:
                logger.info(f'Encountered error while executing OpenBB select: {str(e)}')

                #  TODO: This one doesn't work because it's taken care of from MindsDB side
                if "Table not found" in str(e):
                    raise Exception(f"{str(e)}\n\nCheck if the method exists here: {func_docs}.\n\n  -  If it doesn't you may need to look for the parent module to check whether there's a typo in the naming.\n- If it does you may need to install a new extension to the OpenBB Platform, and you can see what is available at https://my.openbb.co/app/platform/extensions.") from e

                if "Missing credential" in str(e):
                    raise Exception(f"{str(e)}\n\nGo to https://my.openbb.co/app/platform/api-keys to set this API key, for free.") from e

                # Catch all other errors
                # Create docstring for the current function
                text = "Docstring:"
                for param in params_metadata['fields']:
                    field = params_metadata['fields'][param]
                    if getattr(field.annotation, '__origin__', None) is Union:
                        annotation = f"Union[{', '.join(arg.__name__ for arg in field.annotation.__args__)}]"
                    else:
                        annotation = field.annotation.__name__
                    text += f"\n  * {param}{'' if field.is_required() else ' (optional)'}: {annotation}\n{field.description}"

                text += f"\n\nFor more information check {func_docs}"

                raise Exception(f"{str(e)}\n\n{text}.") from e

        def get_columns(self):
            return response_columns

    return AnyTable
