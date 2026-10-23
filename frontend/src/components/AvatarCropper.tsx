import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

const STAGE = 240;  // on-screen crop area, px
const MAX_ZOOM = 4;
const KEY_STEP = 10;

type Position = { x: number; y: number };
type Props = { file: File; outputSize: number; saving: boolean; saveError: string; onCancel: () => void; onSave: (dataUrl: string) => void };

/** Let the user drag and zoom a picture inside a circle, then crop it. */
function AvatarCropper({ file, outputSize, saving, saveError, onCancel, onSave }: Props) {
  const [source, setSource] = useState('');
  const [image, setImage] = useState<HTMLImageElement | null>(null);
  const [error, setError] = useState('');
  const [zoom, setZoom] = useState(1);
  const [position, setPosition] = useState<Position>({ x: 0, y: 0 });
  const drag = useRef<{ pointer: number; start: Position; from: Position } | null>(null);
  const [dragging, setDragging] = useState(false);
  const stage = useRef<HTMLDivElement>(null);

  useEffect(() => { stage.current?.focus(); }, []);

  useEffect(() => {
    const url = URL.createObjectURL(file);
    let current = true;  // a cancelled load (its URL revoked) must not report an error
    setSource(url);
    const element = new Image();
    element.onload = () => {
      if (!current) return;
      setImage(element); setError('');
      const size = coverSize(element, 1);
      setPosition({ x: (STAGE - size.width) / 2, y: (STAGE - size.height) / 2 });
    };
    element.onerror = () => { if (current) setError('That file is not an image this browser can read.'); };
    element.src = url;
    return () => { current = false; URL.revokeObjectURL(url); };
  }, [file]);

  // Keep the picture covering the whole circle's square.
  function clamp(next: Position, scale = zoom): Position {
    if (!image) return next;
    const { width, height } = coverSize(image, scale);
    return {
      x: Math.min(0, Math.max(STAGE - width, next.x)),
      y: Math.min(0, Math.max(STAGE - height, next.y)),
    };
  }

  // Zoom around the centre of the circle so the part being looked at stays put.
  function changeZoom(next: number) {
    if (!image) return;
    const scale = Math.min(MAX_ZOOM, Math.max(1, next));
    const ratio = scale / zoom;
    const centre = STAGE / 2;
    setPosition(clamp({ x: centre - (centre - position.x) * ratio, y: centre - (centre - position.y) * ratio }, scale));
    setZoom(scale);
  }

  function save() {
    if (!image) return;
    const shown = coverSize(image, zoom);
    const factor = image.naturalWidth / shown.width;  // screen px -> image px
    const canvas = Object.assign(document.createElement('canvas'), { width: outputSize, height: outputSize });
    const context = canvas.getContext('2d');
    if (!context) { setError('This browser cannot resize images.'); return; }
    context.drawImage(image, -position.x * factor, -position.y * factor, STAGE * factor, STAGE * factor, 0, 0, outputSize, outputSize);
    const webp = canvas.toDataURL('image/webp', 0.86);
    // Browsers without WebP encoding return PNG; JPEG is much smaller then.
    onSave(webp.startsWith('data:image/webp') ? webp : canvas.toDataURL('image/jpeg', 0.86));
  }

  const shown = image ? coverSize(image, zoom) : null;
  // A portal: the settings dialog is transformed, which would trap a fixed overlay inside it.
  return createPortal(<div className="modal-backdrop cropper-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget && !saving) onCancel(); }}
    onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); if (!saving) onCancel(); } }}>
    <section className="confirm-modal cropper-modal" role="dialog" aria-modal="true" aria-labelledby="cropper-title">
      <span className="eyebrow">PROFILE PICTURE</span>
      <h2 id="cropper-title">Adjust your picture</h2>
      <p>Drag to move it, and zoom to choose how much fits in the circle.</p>
      <div ref={stage} className={`crop-stage ${dragging ? 'dragging' : ''}`} tabIndex={0} aria-label="Picture position. Use the arrow keys to move it."
        onPointerDown={event => {
          if (!image) return;
          event.currentTarget.setPointerCapture(event.pointerId);
          drag.current = { pointer: event.pointerId, start: { x: event.clientX, y: event.clientY }, from: position };
          setDragging(true);
        }}
        onPointerMove={event => {
          const current = drag.current;
          if (!current || current.pointer !== event.pointerId) return;
          setPosition(clamp({ x: current.from.x + event.clientX - current.start.x, y: current.from.y + event.clientY - current.start.y }));
        }}
        onPointerUp={() => { drag.current = null; setDragging(false); }} onPointerCancel={() => { drag.current = null; setDragging(false); }}
        onWheel={event => changeZoom(zoom * (event.deltaY < 0 ? 1.08 : 1 / 1.08))}
        onKeyDown={event => {
          const moves: Record<string, Position> = { ArrowLeft: { x: KEY_STEP, y: 0 }, ArrowRight: { x: -KEY_STEP, y: 0 }, ArrowUp: { x: 0, y: KEY_STEP }, ArrowDown: { x: 0, y: -KEY_STEP } };
          const move = moves[event.key];
          if (move) { event.preventDefault(); setPosition(clamp({ x: position.x + move.x, y: position.y + move.y })); }
          if (event.key === '+' || event.key === '=') changeZoom(zoom + 0.2);
          if (event.key === '-') changeZoom(zoom - 0.2);
        }}>
        {source && shown && <img src={source} alt="" draggable={false} style={{ width: shown.width, height: shown.height, transform: `translate(${position.x}px, ${position.y}px)` }}/>}
        <span className="crop-ring" aria-hidden="true"/>
      </div>
      <label className="crop-zoom">
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/></svg>
        <input type="range" min={1} max={MAX_ZOOM} step={0.01} value={zoom} onChange={event => changeZoom(Number(event.target.value))} aria-label="Zoom" disabled={!image}/>
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/></svg>
      </label>
      {(error || saveError) && <div className="error-box" role="alert"><span>{error || saveError}</span></div>}
      <div className="modal-actions">
        <button className="cancel-button" type="button" onClick={onCancel} disabled={saving}>Cancel</button>
        <button className="send-button" type="button" onClick={save} disabled={!image || saving}>{saving ? 'Saving…' : 'Save picture'}</button>
      </div>
    </section>
  </div>, document.body);
}

/** Size at which the image just covers the stage, times ``zoom``. */
function coverSize(image: HTMLImageElement, zoom: number) {
  const scale = (STAGE / Math.min(image.naturalWidth, image.naturalHeight)) * zoom;
  return { width: image.naturalWidth * scale, height: image.naturalHeight * scale };
}

export default AvatarCropper;
