"""Two fictional E50 observations sharing two keys in two monthly partitions."""
from copy import deepcopy
from datetime import timedelta
from uuid import UUID

from tests.test_data_store_domain_samples import sample, NOW, DAY_MS
from app.data_ingestion.tonghuashun.confirmation import returned_keys
from app.data_store.adapters.contracts import digest, native_json


def overlapping_observations(*, first_confirmed=False, second_confirmed=False):
    body = deepcopy(sample('E50').content)
    body['thscode'] = 'R01-FICTION.SH'
    point = dict(body['item'][0], thscode=body['thscode'])
    body['item'] = [dict(point, date_ms=DAY_MS),
                    dict(point, date_ms=DAY_MS + 31 * 86400000)]
    observations = []
    for number, confirmed in enumerate((first_confirmed, second_confirmed), 1):
        # An already merged fictional import needs exact returned-key evidence.
        # The second proven receipt returns January only; February is inherited.
        request = {'parameters': {}, 'artifact_sha256': digest('fictional-r01-import'),
                   'source_observed_at': (NOW + timedelta(seconds=number)).isoformat()}
        if confirmed:
            request.update(returned_keys({'item': body['item'] if number == 1 else body['item'][:1]}))
        observations.append(dict(
            id=str(UUID(int=number)), dataset='stock_daily', subject=body['thscode'],
            variant='default', observed_at=NOW + timedelta(seconds=10 * number),
            content_hash=digest(body), data_json=native_json(body),
            request_json=native_json([request]), base_observation_id=None))
    return body, observations
