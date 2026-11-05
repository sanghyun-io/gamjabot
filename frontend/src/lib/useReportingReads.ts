import { useCallback, useEffect, useRef, useState } from 'react'
import { getReportStatus, listReports, type ReportSnapshot, type ReportStatus } from './reporting'

type ReadPhase = 'loading' | 'ready' | 'error'

// Native reads cannot be aborted. Each channel owns a token so a superseded reply is ignored.
// No report/consent mutation, background polling, or persistent diagnostic cache lives here.
export function useReportingReads() {
  const [status, setStatus] = useState<ReportStatus | null>(null)
  const [snapshot, setSnapshot] = useState<ReportSnapshot | null>(null)
  const [statusPhase, setStatusPhase] = useState<ReadPhase>('loading')
  const [snapshotPhase, setSnapshotPhase] = useState<ReadPhase>('loading')
  const [statusReceivedAt, setStatusReceivedAt] = useState<number | null>(null), [snapshotReceivedAt, setSnapshotReceivedAt] = useState<number | null>(null)
  const mounted = useRef(false)
  const statusRun = useRef(0), snapshotRun = useRef(0)
  const statusBusy = useRef(false), snapshotBusy = useRef(false)
  const readStatus = useCallback(async (supersede = false): Promise<ReportStatus | null> => {
    if (!mounted.current || statusBusy.current && !supersede) return null
    const run = ++statusRun.current
    statusBusy.current = true; setStatusPhase('loading')
    try {
      const value = await getReportStatus()
      if (!mounted.current || run !== statusRun.current) return null
      setStatus(value); setStatusReceivedAt(Date.now()); setStatusPhase('ready'); return value
    } catch {
      if (mounted.current && run === statusRun.current) setStatusPhase('error')
      return null
    } finally {
      if (run === statusRun.current) statusBusy.current = false
    }
  }, [])
  const readSnapshot = useCallback(async (supersede = false): Promise<ReportSnapshot | null> => {
    if (!mounted.current || snapshotBusy.current && !supersede) return null
    const run = ++snapshotRun.current
    snapshotBusy.current = true; setSnapshotPhase('loading')
    try {
      const value = await listReports()
      if (!mounted.current || run !== snapshotRun.current) return null
      setSnapshot(value); setSnapshotReceivedAt(Date.now()); setSnapshotPhase('ready'); return value
    } catch {
      if (mounted.current && run === snapshotRun.current) setSnapshotPhase('error')
      return null
    } finally {
      if (run === snapshotRun.current) snapshotBusy.current = false
    }
  }, [])
  const invalidateReads = useCallback(() => {
    ++statusRun.current; ++snapshotRun.current
    statusBusy.current = false; snapshotBusy.current = false
    setStatusPhase('loading'); setSnapshotPhase('loading')
  }, [])
  useEffect(() => {
    mounted.current = true
    void readStatus(); void readSnapshot()
    return () => {
      mounted.current = false; ++statusRun.current; ++snapshotRun.current
      statusBusy.current = false; snapshotBusy.current = false
    }
  }, [readStatus, readSnapshot])
  return { status, snapshot, statusPhase, snapshotPhase, statusReceivedAt, snapshotReceivedAt, readStatus, readSnapshot, invalidateReads }
}

export type ReportingReads = ReturnType<typeof useReportingReads>
