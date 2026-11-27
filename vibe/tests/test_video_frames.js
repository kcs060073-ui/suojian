const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let revoked = false;
let closed = false;
const video = {
 duration: 12, videoWidth: 160, videoHeight: 90, listeners: {},
 addEventListener(name, fn) { this.listeners[name] = fn; },
 removeEventListener(name) { delete this.listeners[name]; },
 set src(value) { setImmediate(() => this.listeners.loadedmetadata()); },
 set currentTime(value) { this.time = value; setImmediate(() => this.listeners.seeked()); },
 removeAttribute() {}, load() { closed = true; }
};
const canvas = {getContext() {return {drawImage() {}}}, toBlob(callback) {callback({type:'image/jpeg'})}};
const context = {window:{}, document:{createElement(name) {return name === 'video' ? video : canvas}},
 URL:{createObjectURL(){return 'blob:test'}, revokeObjectURL(){revoked=true}}, setTimeout, clearTimeout, Number, Math, Error};
vm.runInNewContext(fs.readFileSync('workbench/static/video-frames.js', 'utf8'), context);
(async () => {
 const result = await context.window.extractSceneFrames({}, 3);
 assert.equal(result.duration, 12);
 assert.deepEqual(Array.from(result.frames, frame => frame.seconds), [2, 6, 10]);
 assert.ok(result.frames.every(frame => frame.blob.type === 'image/jpeg'));
 assert.equal(revoked, true);
 assert.equal(closed, true);
 console.log('video frame timing and cleanup: passed');
})().catch(error => {console.error(error); process.exitCode = 1});
