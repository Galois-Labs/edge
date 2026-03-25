// gen-icons generates .ico icon files for the system tray application.
// It produces green.ico (online), yellow.ico (degraded), and red.ico (offline).
package main

import (
	"encoding/binary"
	"image"
	"image/color"
	"image/draw"
	"math"
	"os"
	"path/filepath"
)

const (
	iconSize = 32
	radius   = 13.0 // circle radius in pixels
	centerX  = 15.5 // center of 32x32 image
	centerY  = 15.5
)

type iconDef struct {
	filename string
	fill     color.RGBA
	border   color.RGBA
}

func main() {
	outDir := filepath.Join("internal", "tray", "assets")
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		panic(err)
	}

	icons := []iconDef{
		{
			filename: "green.ico",
			fill:     color.RGBA{R: 0x00, G: 0xC8, B: 0x53, A: 0xFF}, // #00C853
			border:   color.RGBA{R: 0x00, G: 0x7E, B: 0x33, A: 0xFF},
		},
		{
			filename: "yellow.ico",
			fill:     color.RGBA{R: 0xFF, G: 0xB3, B: 0x00, A: 0xFF}, // #FFB300
			border:   color.RGBA{R: 0xC6, G: 0x8A, B: 0x00, A: 0xFF},
		},
		{
			filename: "red.ico",
			fill:     color.RGBA{R: 0xFF, G: 0x17, B: 0x44, A: 0xFF}, // #FF1744
			border:   color.RGBA{R: 0xC4, G: 0x00, B: 0x23, A: 0xFF},
		},
	}

	for _, ic := range icons {
		img := renderCircle(ic.fill, ic.border)
		data := encodeICO(img)
		path := filepath.Join(outDir, ic.filename)
		if err := os.WriteFile(path, data, 0o644); err != nil {
			panic(err)
		}
	}
}

// renderCircle draws an anti-aliased colored circle with a 1px dark border
// on a transparent 32x32 RGBA image.
func renderCircle(fill, border color.RGBA) *image.RGBA {
	img := image.NewRGBA(image.Rect(0, 0, iconSize, iconSize))
	draw.Draw(img, img.Bounds(), image.Transparent, image.Point{}, draw.Src)

	outerR := radius
	innerR := radius - 1.0

	for y := 0; y < iconSize; y++ {
		for x := 0; x < iconSize; x++ {
			dx := float64(x) - centerX
			dy := float64(y) - centerY
			dist := math.Sqrt(dx*dx + dy*dy)

			if dist <= innerR-0.5 {
				// Fully inside the fill area
				img.SetRGBA(x, y, fill)
			} else if dist <= innerR+0.5 {
				// Anti-aliased edge between fill and border
				t := innerR + 0.5 - dist // 1.0 = fully fill, 0.0 = fully border
				img.SetRGBA(x, y, lerpColor(border, fill, t))
			} else if dist <= outerR-0.5 {
				// Fully inside the border ring
				img.SetRGBA(x, y, border)
			} else if dist <= outerR+0.5 {
				// Anti-aliased outer edge (border to transparent)
				t := outerR + 0.5 - dist // 1.0 = fully border, 0.0 = fully transparent
				img.SetRGBA(x, y, color.RGBA{
					R: border.R,
					G: border.G,
					B: border.B,
					A: uint8(float64(border.A) * t),
				})
			}
			// else: outside the circle, stays transparent
		}
	}
	return img
}

func lerpColor(a, b color.RGBA, t float64) color.RGBA {
	return color.RGBA{
		R: uint8(float64(a.R)*(1-t) + float64(b.R)*t),
		G: uint8(float64(a.G)*(1-t) + float64(b.G)*t),
		B: uint8(float64(a.B)*(1-t) + float64(b.B)*t),
		A: uint8(float64(a.A)*(1-t) + float64(b.A)*t),
	}
}

// encodeICO produces a valid Windows .ico file containing one 32x32 32-bit image.
func encodeICO(img *image.RGBA) []byte {
	const (
		headerSize    = 6
		dirEntrySize  = 16
		bmpHeaderSize = 40
	)

	w := iconSize
	h := iconSize

	// Pixel data: BGRA, bottom-up row order
	pixelDataSize := w * h * 4
	// AND mask: 1 bit per pixel, rows padded to 4 bytes
	andRowBytes := ((w + 31) / 32) * 4 // 4 bytes per row for 32px width
	andMaskSize := andRowBytes * h

	bitmapDataSize := bmpHeaderSize + pixelDataSize + andMaskSize
	imageOffset := headerSize + dirEntrySize

	buf := make([]byte, imageOffset+bitmapDataSize)

	// --- ICO Header (6 bytes) ---
	binary.LittleEndian.PutUint16(buf[0:2], 0)    // reserved
	binary.LittleEndian.PutUint16(buf[2:4], 1)    // type = icon
	binary.LittleEndian.PutUint16(buf[4:6], 1)    // count = 1 image

	// --- Directory Entry (16 bytes) ---
	off := headerSize
	buf[off+0] = byte(w)  // width (32)
	buf[off+1] = byte(h)  // height (32)
	buf[off+2] = 0        // color count
	buf[off+3] = 0        // reserved
	binary.LittleEndian.PutUint16(buf[off+4:off+6], 1)                    // planes
	binary.LittleEndian.PutUint16(buf[off+6:off+8], 32)                   // bit count
	binary.LittleEndian.PutUint32(buf[off+8:off+12], uint32(bitmapDataSize)) // bytes in resource
	binary.LittleEndian.PutUint32(buf[off+12:off+16], uint32(imageOffset))   // image offset

	// --- BITMAPINFOHEADER (40 bytes) ---
	bmp := buf[imageOffset:]
	binary.LittleEndian.PutUint32(bmp[0:4], bmpHeaderSize)       // size
	binary.LittleEndian.PutUint32(bmp[4:8], uint32(w))           // width
	binary.LittleEndian.PutUint32(bmp[8:12], uint32(h*2))        // height (double: XOR + AND)
	binary.LittleEndian.PutUint16(bmp[12:14], 1)                 // planes
	binary.LittleEndian.PutUint16(bmp[14:16], 32)                // bit count
	binary.LittleEndian.PutUint32(bmp[16:20], 0)                 // compression = BI_RGB
	binary.LittleEndian.PutUint32(bmp[20:24], uint32(pixelDataSize+andMaskSize)) // image size
	// bytes 24-39: xPelsPerMeter, yPelsPerMeter, clrUsed, clrImportant = 0

	// --- Pixel data: BGRA, bottom-up ---
	pixelStart := imageOffset + bmpHeaderSize
	for row := 0; row < h; row++ {
		srcY := h - 1 - row // bottom-up: first written row is the bottom of the image
		for col := 0; col < w; col++ {
			r, g, b, a := img.At(col, srcY).RGBA()
			idx := pixelStart + (row*w+col)*4
			buf[idx+0] = uint8(b >> 8) // B
			buf[idx+1] = uint8(g >> 8) // G
			buf[idx+2] = uint8(r >> 8) // R
			buf[idx+3] = uint8(a >> 8) // A
		}
	}

	// --- AND mask: 1-bit, bottom-up ---
	// For 32-bit icons with alpha, the AND mask should reflect transparency.
	// Bit=1 means transparent, bit=0 means opaque.
	andStart := pixelStart + pixelDataSize
	for row := 0; row < h; row++ {
		srcY := h - 1 - row
		for col := 0; col < w; col++ {
			_, _, _, a := img.At(col, srcY).RGBA()
			if a == 0 {
				// transparent pixel: set bit to 1
				byteIdx := andStart + row*andRowBytes + col/8
				bitIdx := 7 - (col % 8)
				buf[byteIdx] |= 1 << uint(bitIdx)
			}
		}
	}

	return buf
}
