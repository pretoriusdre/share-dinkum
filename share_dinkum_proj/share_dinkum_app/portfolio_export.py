"""The Excel workbook of a portfolio's records, which can be loaded back into an empty portfolio.

Written by the DataExport signal (the dashboard's Export portfolio button) and by the full
backup, which puts one beside the database so the records stay readable without this app.
"""

import logging
from pathlib import Path

from django.apps import apps

from share_dinkum_app import column_help, excelinterface, loading
from share_dinkum_app.models import Account, InstrumentPriceHistory
from share_dinkum_app.reports import RealisedCapitalGainReport

logger = logging.getLogger(__name__)


def write_workbook(account: Account, path: str | Path, include_price_history: bool = False) -> None:
    """Write `account`'s records to the Excel file at `path`.

    Price history is left out unless asked for, since the market can supply it again.
    """
    gen = excelinterface.ExcelGen(title='Data Export')
    for model in apps.get_app_config('share_dinkum_app').get_models():

        if model == InstrumentPriceHistory and not include_price_history:
            continue

        logger.info('    - %s', model.__name__)

        if 'account' in [f.name for f in model._meta.get_fields()]:
            queryset = loading.model_to_queryset(model=model, account=account)
        elif model is Account:
            # Only this portfolio: a file naming several cannot be restored on its own.
            queryset = loading.model_to_queryset(model=model).filter(pk=account.pk)
        else:
            queryset = loading.model_to_queryset(model=model)

        df = loading.queryset_to_df(queryset)
        desc = getattr(model, 'MODEL_DESCRIPTION', 'No description available')
        if not df.empty:
            gen.add_table(df, table_name=model.__name__, description=desc,
                          column_descriptions=column_help.describe_columns(model, [str(c) for c in df.columns]))

    logger.info('    - Realised Capital Gains Report')
    gen.add_table(RealisedCapitalGainReport(account=account).generate(), table_name='RealisedCapitalGains',
                  description='Report of realised capital gains per sale allocation.')

    gen.save(path)
