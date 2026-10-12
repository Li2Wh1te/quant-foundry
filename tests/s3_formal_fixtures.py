"""Private E50/E70 node fixtures, never a production fact/source publisher."""
from datetime import datetime, timezone
from decimal import Decimal
from app.backtest_service.formal_runtime import daily_node_binding, REQUIRED_EXECUTION_FACTS
from app.data_store.adapters.contracts import LocalInput, digest
from app.data_store.adapters.normalize import normalize
from app.data_store.adapters.registry import BY_ID
from app.data_store.pipeline import run_entry
from support_sdk import spec

MODEL = 'synthetic_d18_formal_node_oracles'
NOW = datetime(2026,1,10,tzinfo=timezone.utc)


class Inputs:
    def __init__(self,*rows):self.rows,self.summary=rows,{}
    def iter_entry(self,entry):
        self.summary={'complete':False}
        yield from self.rows
        self.summary={'complete':True,'source_rows':len(self.rows),'state':'present' if self.rows else 'empty'}


def nodes(store, entry_id='E70', *, quantities=None, securities=('A.SH','B.SH')):
    entry = BY_ID[entry_id]
    packet = spec(days=3,securities=securities)
    packet['synthetic_model'] = MODEL
    # Keep the original fee applicability guard: its named model must equal
    # the run's declared model. A renamed fixture is not a zero-fee override.
    for rule in packet['rules']:
        rule['origin'] = dict(kind='synthetic',reference=MODEL)
    for fact in packet['facts']:
        fact['origin'] = dict(kind='synthetic',reference=MODEL)
        for side in ('buy','sell'):
            fact[side+'_fees']['synthetic_model'] = MODEL
    identifiers = {s:('NATIVE-'+s,'isolated-stable-'+s) for s in securities}
    inputs = []
    for i,row in enumerate(packet['calendar']):
        for subject,_ in identifiers.values():
            quantity = quantities[i] if quantities is not None else ('10.01' if entry_id=='E70' else '1001')
            if entry_id=='E70':
                body = dict(source='tushare',ts_code=subject,trade_date=row['date'],
                    open=Decimal('10'),high=Decimal('11'),low=Decimal('9'),close=Decimal('10'),
                    vol=None if quantity is None else Decimal(quantity))
            else:
                body = dict(adjust='none',coverage='observed_rows_only',item=[dict(
                    date_ms=int(row['local_midnight_ns'])//1_000_000,
                    open_price='10',high_price='11',low_price='9',close_price='10',volume=quantity,turnover=None)])
            inputs.append(LocalInput(entry.source,entry.native,subject,MODEL,NOW,body,digest(body)))
    outcome = run_entry(store,entry,Inputs(*inputs))
    assert outcome['qualified'],outcome
    representation = next(normalize(entry,inputs[0])).representation
    binding = daily_node_binding(entry_id,representation=representation,identities=identifiers,
        calendar=packet['calendar'],shares_per_quantity='100' if entry_id=='E70' else '1',
        accepted_facts=REQUIRED_EXECUTION_FACTS,evidence={'kind':'synthetic','reference':MODEL})
    packet['source'] = dict(kind='ipc',socket_path='/run/qf/ipc/data.sock',binding=binding.name,request={})
    return binding,packet
