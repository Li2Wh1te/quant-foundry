"""One authenticated, read-only entry point for HTTP and Python consumers.

Session lifetime is the guard lifetime: serialize before returning and let the
adapter finish the transaction afterwards. No method registers data or work.
"""
import json
from uuid import UUID
from pydantic import Field, BaseModel, ConfigDict
from app.core.auth import AuthenticatedPrincipal
from app.data_foundation.canonical import FoundationError, encode, digest
from app.data_foundation.query import DataRequirement, query_official, resolve_release, dataset_view, lineage
from app.data_foundation.projection import projection_for
from app.data_foundation.resolution import ResolutionCodec, token_hash
from app.data_foundation.catalog import now


class PagedRequirement(DataRequirement):
    paging_version: int = Field(ge=1,le=1)
    page_size: int = Field(default=100,ge=1,le=1000)


class PageRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    resolution_token: str = Field(max_length=32768)
    cursor: str | None = Field(default=None,max_length=2048)
    excluded_cursor: str | None = Field(default=None,max_length=2048)
    page_size: int = Field(default=100,ge=1,le=1000)


class FoundationService:
    def __init__(self, session, authenticate, secret, *, clock=None):
        self.session=session
        self.authenticate=authenticate
        self.codec=ResolutionCodec(secret,**({'clock':clock} if clock else {}))
        self.principal=self._principal()

    def _principal(self):
        principal=self.authenticate()
        if not isinstance(principal,AuthenticatedPrincipal) or not principal.owner_scope:
            raise FoundationError('AUTH_REQUIRED','底座读取需要由认证适配器核验的身份。')
        return principal

    def owner(self):
        principal=self._principal()
        if principal!=self.principal:
            raise FoundationError('AUTH_CONTEXT_CHANGED','当前身份已失效，请重新登录。')
        return principal.owner_scope

    def finish(self, value):
        self.owner()
        return encode(value).encode()

    def describe_dataset(self):
        result=dataset_view(self.session)
        projection=projection_for(self.session,result['dataset'],result['version'])
        result['projection']={k:v for k,v in projection.items() if k!='definition'}
        result['contract']=projection['definition']
        result['support']={'read':projection['read_status'],'update':'bounded_manual',
            'replay':'see_source_execution','time_modes':['observed'],'max_subject_calendar_days':1000}
        return self.finish(result)

    def check_capability(self, request, *, snapshot_hash=None):
        return self.finish(self._resolve(request, snapshot_hash=snapshot_hash))

    def _resolve(self, request, *, snapshot_hash=None):
        projection=projection_for(self.session,request.dataset_id,request.contract_version)
        response=json.loads(query_official(self.session,request,self.owner,check_only=True,page_size=100))
        response.update(projection_version=projection['version'],projection_hash=projection['hash'])
        response['resolution_token']=None
        if response['release_id']:
            fixed=request.model_copy(update={'release':UUID(response['release_id'])})
            normalized=fixed.model_dump(mode='json',by_alias=True)
            issued=int(self.codec.clock())
            payload={'request':normalized,'request_hash':digest('request-v1',normalized),
                'release_id':response['release_id'],'manifest_hash':response['manifest_hash'],
                'projection_hash':projection['hash'],'issue_epoch':response['issue_state_version'],
                'owner_hash':digest('owner-v1',self.owner()),'sort':projection['sort'],
                'issued_at':issued,'expires_at':issued+900,'assessment_hash':response['assessment_hash'],
                'snapshot_hash':snapshot_hash}
            response['request']=normalized
            response['resolution_token']=self.codec.sign('resolution',payload)
            response['expires_at']=issued+900
            response['snapshot_hash']=snapshot_hash
            response['next_excluded_cursor']=None
            if response.get('excluded_has_more') and response['excluded']:
                last=response['excluded'][-1]
                response['next_excluded_cursor']=self.codec.sign('excluded',{
                    'resolution_hash':token_hash(response['resolution_token']),
                    'after':[last['trade_date'],last['instrument_id'],last['reason']]})
        return response

    def query_official(self, request):
        if isinstance(request,PageRequest):
            page=request
        elif isinstance(request,PagedRequirement):
            plain=DataRequirement.model_validate(request.model_dump(exclude={'paging_version','page_size'}))
            check=self._resolve(plain)
            if not check['resolution_token']:return self.finish(check)
            page=PageRequest(resolution_token=check['resolution_token'],page_size=request.page_size)
        else:
            projection_for(self.session,request.dataset_id,request.contract_version)
            # Keep M3's complete, bounded response for callers not opting in.
            return query_official(self.session,request,self.owner)
        payload=self.codec.read(page.resolution_token,'resolution')
        if payload['owner_hash']!=digest('owner-v1',self.owner()):
            raise FoundationError('AUTH_CONTEXT_CHANGED','检查凭据不属于当前身份，请重新检查。')
        fixed=DataRequirement.model_validate(payload['request'])
        projection=projection_for(self.session,fixed.dataset_id,fixed.contract_version)
        release=resolve_release(self.session,fixed)
        if (release is None or str(release.id)!=payload['release_id'] or release.manifest_hash!=payload['manifest_hash']
            or projection['hash']!=payload['projection_hash'] or payload['request_hash']!=digest('request-v1',payload['request'])):
            raise FoundationError('INVALID_RESOLUTION','固定版本或读取投影不匹配，请重新检查原版本。')
        def after(cursor,kind):
            if not cursor:return None
            item=self.codec.read(cursor,kind)
            if item['resolution_hash']!=token_hash(page.resolution_token):
                raise FoundationError('CURSOR_MISMATCH','页游标不属于本次检查，请从第一页重新读取。')
            return item['after']
        result=json.loads(query_official(self.session,fixed,self.owner,page_size=page.page_size,
            page_after=after(page.cursor,'cursor'),excluded_after=after(page.excluded_cursor,'excluded'),
            expected_issue_epoch=payload['issue_epoch']))
        def cursor(kind,last):
            return self.codec.sign(kind,{'resolution_hash':token_hash(page.resolution_token),'after':last})
        result.update(resolution_token=page.resolution_token,expires_at=payload['expires_at'],
            projection_hash=projection['hash'],projection_version=projection['version'],snapshot_hash=payload.get('snapshot_hash'))
        if result.get('has_more') and result['items']:
            last=result['items'][-1]
            result['next_cursor']=cursor('cursor',[last['trade_date'],last['instrument_id']])
        result['next_excluded_cursor']=None
        if result.get('excluded_has_more') and result['excluded']:
            last=result['excluded'][-1]
            result['next_excluded_cursor']=cursor('excluded',[last['trade_date'],last['instrument_id'],last['reason']])
        return self.finish(result)

    def resolve_snapshot(self, requests):
        if not 1<=len(requests)<=8:
            raise FoundationError('INVALID_REQUIREMENT','组合快照需要1至8个请求。')
        # Resolve every head in one SQL statement so READ COMMITTED callers
        # also obtain a single committed head view, without stale issue locks.
        from sqlalchemy import select
        from app.data_foundation.work import scope_key
        from app.data_foundation.work_models import Head
        scopes=[scope_key(r.dataset_id,int(r.contract_version.split('.')[0]),r.profile_id,r.semantic_series_id) for r in requests]
        heads={h.scope_key:h.release_id for h in self.session.scalars(select(Head).where(Head.scope_key.in_(scopes)))}
        entries=[]
        for request,scope in zip(requests,scopes):
            fixed=request
            if request.release=='latest':
                if scope not in heads:raise FoundationError('RELEASE_UNAVAILABLE','快照条目尚无正式发布。')
                fixed=request.model_copy(update={'release':heads[scope]})
            p=projection_for(self.session,fixed.dataset_id,fixed.contract_version)
            release=resolve_release(self.session,fixed)
            entries.append({'request':fixed.model_dump(mode='json',by_alias=True),
                'manifest_hash':release.manifest_hash,'projection_hash':p['hash']})
        body={'schema':1,'entries':entries}
        return self.finish({**body,'snapshot_hash':digest('snapshot-v1',body)})

    def read_snapshot(self, snapshot):
        if set(snapshot)!={'schema','entries','snapshot_hash'} or snapshot['schema']!=1 or not 1<=len(snapshot['entries'])<=8:
            raise FoundationError('INVALID_REQUIREMENT','快照描述格式无效。')
        body={k:snapshot[k] for k in ('schema','entries')}
        if digest('snapshot-v1',body)!=snapshot['snapshot_hash']:
            raise FoundationError('INVALID_REQUIREMENT','快照描述摘要不匹配。')
        results=[]
        # Shared issue guards remain held until every entry has been serialized.
        for entry in snapshot['entries']:
            request=DataRequirement.model_validate(entry['request'])
            if request.release=='latest':raise FoundationError('INVALID_REQUIREMENT','快照必须指定正式版本。')
            p=projection_for(self.session,request.dataset_id,request.contract_version)
            release=resolve_release(self.session,request)
            if entry['manifest_hash']!=release.manifest_hash or entry['projection_hash']!=p['hash']:
                raise FoundationError('CONTRACT_RELEASE_MISMATCH','快照正式内容或投影不匹配。')
            results.append(self._resolve(request,snapshot_hash=snapshot['snapshot_hash']))
        return self.finish({'snapshot_hash':snapshot['snapshot_hash'],'items':results,'checked_at':now()})

    def get_lineage(self, revision_id):
        return self.finish(lineage(self.session,revision_id))
