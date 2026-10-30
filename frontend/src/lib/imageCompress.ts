/**
 * 前端图片压缩：长边 ≤2048px、统一转 JPEG（质量 0.8），目标 ≤1MB。
 *
 * 在浏览器里用 Canvas 完成，不引入额外依赖；压缩后再上传，
 * 避免 10MB 原图直接变成巨大的 base64 请求体。
 */

export const MAX_IMAGE_EDGE = 2048
export const MAX_IMAGE_BYTES = 1024 * 1024
const INITIAL_QUALITY = 0.8
const MIN_QUALITY = 0.5

function loadImage(file: File): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const objectUrl = URL.createObjectURL(file)
    const image = new Image()
    image.onload = () => {
      URL.revokeObjectURL(objectUrl)
      resolve(image)
    }
    image.onerror = () => {
      URL.revokeObjectURL(objectUrl)
      reject(new Error('图片读取失败，请换一张图片'))
    }
    image.src = objectUrl
  })
}

function canvasToBlob(canvas: HTMLCanvasElement, quality: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error('图片压缩失败'))),
      'image/jpeg',
      quality,
    )
  })
}

function drawResized(image: HTMLImageElement, scale: number): HTMLCanvasElement {
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(image.naturalWidth * scale))
  canvas.height = Math.max(1, Math.round(image.naturalHeight * scale))
  const context = canvas.getContext('2d')
  if (!context) throw new Error('当前浏览器不支持 Canvas 图片压缩')
  context.fillStyle = '#ffffff'
  context.fillRect(0, 0, canvas.width, canvas.height)
  context.drawImage(image, 0, 0, canvas.width, canvas.height)
  return canvas
}

/** 压缩成 JPEG；超出 1MB 时先降质量，再逐级缩小尺寸。 */
export async function compressImageFile(file: File): Promise<File> {
  if (!file.type.startsWith('image/')) {
    throw new Error('只支持图片文件')
  }
  const image = await loadImage(file)
  const maxEdge = Math.max(image.naturalWidth, image.naturalHeight)
  if (maxEdge <= 0) throw new Error('图片尺寸异常')

  let scale = Math.min(1, MAX_IMAGE_EDGE / maxEdge)
  let quality = INITIAL_QUALITY
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const canvas = drawResized(image, scale)
    const blob = await canvasToBlob(canvas, quality)
    if (blob.size <= MAX_IMAGE_BYTES) {
      const base = file.name.replace(/\.[^.]+$/, '') || 'image'
      return new File([blob], `${base}.jpg`, { type: 'image/jpeg', lastModified: Date.now() })
    }
    if (quality > MIN_QUALITY) {
      quality = Math.max(MIN_QUALITY, quality - 0.1)
    } else {
      scale *= 0.85
    }
  }
  throw new Error('图片压缩后仍超过 1MB，请换一张更小的图片')
}