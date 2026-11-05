// @vitest-environment jsdom
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import LibraryPins from './LibraryPins'

describe('LibraryPins', () => {
  it('keeps unavailable shortcuts removable and opens only verified originals', () => {
    const remove = vi.fn()
    render(<LibraryPins ko items={[
      { key: 'a', title: '계획', detail: 'Page · v2', availability: 'available', open: <a href="/chat/1">열기</a> },
      { key: 'b', title: '사진', detail: '업로드', availability: 'unknown' },
    ]} loading={false} error={false} busy={false} onRefresh={() => {}} onRemove={remove} />)
    expect(screen.getAllByRole('link', { name: '열기' })).toHaveLength(1)
    expect(screen.getByText(/원본 상태를 확인할 수 없습니다/)).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: '고정 해제' })[1])
    expect(remove).toHaveBeenCalledWith('b')
  })
})
