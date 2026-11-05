import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { connectionsApi, type Connection } from '../../api/connections'
import { useAuthStore } from '../../stores/authStore'
import { useI18n } from '../../i18n'
import { ModelPicker } from './ModelPicker'

type Props = { connection: Connection; identity: string; value: string; onChange: (modelId: string) => void; manualLabel?: string; invalid?: boolean; describedBy?: string }

export function SavedModelPicker(props: Props) {
  const { connection, identity } = props
  return <BoundModelPicker key={`${identity}:${connection.id}:${connection.config_revision ?? 0}:${connection.auth_grant_revision ?? 0}`} {...props} />
}

function BoundModelPicker({ connection, identity, value, onChange, manualLabel, invalid, describedBy }: Props) {
  const { t } = useI18n()
  const cache = useQueryClient()
  const key = ['connection-models', identity, connection.id, connection.config_revision ?? 0, connection.auth_grant_revision ?? 0]
  const valid = () => { const state = useAuthStore.getState(); return `${state.user?.id || 0}:${state.epoch}` === identity }
  const query = useQuery({
    queryKey: key, retry: false,
    queryFn: async () => {
      if (!valid()) throw new Error('Owner changed')
      const result = await connectionsApi.models(connection.id, false)
      if (!valid()) throw new Error('Owner changed')
      return result
    },
  })
  const refresh = useMutation({
    retry: false,
    mutationFn: async () => {
      if (!valid()) throw new Error('Owner changed')
      const revisions = await connectionsApi.revisions(connection.id)
      if (!valid()) throw new Error('Owner changed')
      return connectionsApi.models(connection.id, true, {
        configRevision: revisions.config_revision, authGrantRevision: revisions.auth_grant_revision,
      })
    },
    onSuccess: result => { if (valid()) cache.setQueryData(key, result) },
  })
  const error = refresh.error as { response?: { data?: { detail?: { code?: string } } } } | null
  // Keep a received list scoped to this Owner/connection/revision key. A failed
  // read is not a successful refresh and never makes an option newly verified.
  const failedRead = query.isError || refresh.isError
  const retained = query.data !== undefined && failedRead
  const catalog = retained && query.data ? { ...query.data, stale: true } : query.data
  return <div className="stack stack--tight" style={{ minWidth: 0, maxWidth: '100%' }}>
    {retained && <p role="status" className="notice notice--warning">{t('connections.modelListRetained')}</p>}
    <ModelPicker searchScope={key.join(':')} compact invalid={invalid} describedBy={describedBy} catalog={catalog} value={value} defaultModelId={connection.default_model_id}
    busy={query.isFetching || refresh.isPending} canRefresh={connection.enabled && !query.isError}
    loadError={query.isError} refreshError={refresh.isError}
    configurationChanged={error?.response?.data?.detail?.code === 'configuration_changed'}
    onChange={onChange} onRefresh={() => refresh.mutate()} onRetryLoad={() => void query.refetch({ cancelRefetch: false })}
    copy={{ knownModel: t('connections.knownModel'), chooseKnownModel: t('connections.chooseKnownModel'),
      manualModel: manualLabel ?? t('connections.manualModel'), refresh: t('connections.refreshModels'), refreshing: t('connections.refreshingModels'),
      stale: t('connections.modelCacheStale'), truncated: t('connections.modelCacheTruncated'), empty: t('connections.modelCacheEmpty'),
      loadFailure: t('connections.modelLoadFailure'), refreshFailure: t('connections.modelRefreshFailure'),
      configurationChanged: t('connections.modelConfigurationChanged'), retry: t('common.retry'), retryRefresh: t('connections.retryModelRefresh') }} />
  </div>
}
