import { afterEach, describe, expect, it, vi } from 'vitest'
import { compressImageFile } from '../lib/imageCompress'

function stubCanvas(blobs: Blob[]) {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    fillStyle: '#fff',
    fillRect: vi.fn(),
    drawImage: vi.fn(),
  } as unknown as CanvasRenderingContext2D)

  let index = 0
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((callback) => {
    callback(blobs[Math.min(index, blobs.length - 1)])
    index += 1
  })
}

function stubImage(width: number, height: number) {
  class FakeImage {
    naturalWidth = width
    naturalHeight = height
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    set src(_value: string) {
      queueMicrotask(() => this.onload?.())
    }
  }
  vi.stubGlobal('Image', FakeImage)
  vi.stubGlobal('URL', {
    createObjectURL: vi.fn(() => 'blob:source'),
    revokeObjectURL: vi.fn(),
  })
}

describe('compressImageFile', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('非图片直接拒绝', async () => {
    await expect(compressImageFile(new File(['x'], 'a.txt', { type: 'text/plain' }))).rejects.toThrow(
      '只支持图片文件',
    )
  })

  it('长边超过 2048 时缩小，并统一转 JPEG', async () => {
    stubImage(3000, 1000)
    stubCanvas([new Blob([new Uint8Array(500_000)], { type: 'image/jpeg' })])
    const result = await compressImageFile(new File(['x'], 'photo.png', { type: 'image/png' }))
    expect(result.type).toBe('image/jpeg')
    expect(result.name).toBe('photo.jpg')
  })

  it('超过 1MB 时降质量后再试，直到低于上限', async () => {
    stubImage(800, 600)
    stubCanvas([
      new Blob([new Uint8Array(1_200_000)], { type: 'image/jpeg' }),
      new Blob([new Uint8Array(900_000)], { type: 'image/jpeg' }),
    ])
    const result = await compressImageFile(new File(['x'], 'big.png', { type: 'image/png' }))
    expect(result.size).toBeLessThanOrEqual(1024 * 1024)
  })
})
