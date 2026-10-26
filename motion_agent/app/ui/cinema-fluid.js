/* CeRebel fluid background: a slow domain-warped field in night pine, course blue and one warm
   floodlight, drawn behind the landing and the signed-in app (not ?dev=1, which keeps Vanta).
   Rendered at a fraction of screen resolution and stretched, so it stays cheap on phones.
   window.cxFluid.pulse() speeds it up for a moment (file dragged over, upload started);
   window.cxFluid.dim(x) darkens it while there is data on screen. Reduced motion draws one frame. */
(function () {
  if (new URLSearchParams(location.search).has("dev")) return;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const canvas = document.createElement("canvas");
  canvas.id = "cxFluid";
  canvas.setAttribute("aria-hidden", "true");
  document.body.prepend(canvas);
  const gl = canvas.getContext("webgl", { antialias: false, alpha: false, preserveDrawingBuffer: true, powerPreference: "low-power" });
  if (!gl) { canvas.classList.add("is-static"); return; }

  const vs = "attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}";
  const fs = `precision mediump float;
uniform vec2 res;uniform float t;uniform vec2 m;uniform float energy;uniform float dimv;uniform float scroll;
float h(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}
float n(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);
  return mix(mix(h(i),h(i+vec2(1,0)),f.x),mix(h(i+vec2(0,1)),h(i+vec2(1,1)),f.x),f.y);}
float fbm(vec2 p){float v=0.,a=.5;mat2 r=mat2(.8,-.6,.6,.8);for(int i=0;i<5;i++){v+=a*n(p);p=r*p*2.02;a*=.5;}return v;}
void main(){
  vec2 uv=gl_FragCoord.xy/res;vec2 p=uv*vec2(res.x/res.y,1.)*1.6;
  float s=t*.035*(1.+energy*2.5);
  p+= (m-.5)*.35; p.y+=scroll*.6;
  vec2 q=vec2(fbm(p+vec2(0.,s)),fbm(p+vec2(5.2,1.3)-s));
  vec2 r=vec2(fbm(p+3.*q+vec2(1.7,9.2)+s*1.4),fbm(p+3.*q+vec2(8.3,2.8)-s));
  float f=fbm(p+3.5*r);
  vec3 base=vec3(.039,.082,.071), pine=vec3(.071,.227,.180), blue=vec3(.075,.141,.290), lamp=vec3(.910,.788,.541);
  vec3 c=mix(base,pine,smoothstep(.15,.85,f));
  c=mix(c,blue,smoothstep(.45,1.1,length(q))*.6);
  float glow=smoothstep(.62,.95,f*r.x+.25*energy);
  c+=lamp*glow*.42;
  float md=distance(uv,m);c+=lamp*.05*exp(-md*md*9.);
  float v=smoothstep(1.25,.25,distance(uv,vec2(.5,.55)));c*=mix(.45,1.,v);
  c*=1.-dimv*.45;
  c+=(h(gl_FragCoord.xy+fract(t))-.5)*.035;
  gl_FragColor=vec4(c,1.);
}`;
  function sh(type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    return s;
  }
  const prog = gl.createProgram();
  gl.attachShader(prog, sh(gl.VERTEX_SHADER, vs));
  gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { canvas.classList.add("is-static"); return; }
  gl.useProgram(prog);
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, "p");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const u = {};
  ["res", "t", "m", "energy", "dimv", "scroll"].forEach((k) => { u[k] = gl.getUniformLocation(prog, k); });

  let scale = innerWidth < 720 ? 0.28 : 0.4;
  function size() {
    scale = innerWidth < 720 ? 0.28 : 0.4;
    canvas.width = Math.max(2, Math.round(innerWidth * scale));
    canvas.height = Math.max(2, Math.round(innerHeight * scale));
    gl.viewport(0, 0, canvas.width, canvas.height);
    if (reduce) draw(performance.now());
  }

  // Everything eases toward its target, so the field never jumps.
  const mouse = { x: 0.5, y: 0.6, tx: 0.5, ty: 0.6 };
  let energy = 0, energyT = 0, dimv = 0, dimT = 0, scroll = 0;
  addEventListener("pointermove", (e) => { mouse.tx = e.clientX / innerWidth; mouse.ty = 1 - e.clientY / innerHeight; }, { passive: true });
  function draw(now) {
    mouse.x += (mouse.tx - mouse.x) * 0.04;
    mouse.y += (mouse.ty - mouse.y) * 0.04;
    energy += (energyT - energy) * 0.05;
    energyT *= 0.985;
    dimv += (dimT - dimv) * 0.05;
    scroll += (scrollY / Math.max(1, innerHeight) * 0.25 - scroll) * 0.06;
    gl.uniform2f(u.res, canvas.width, canvas.height);
    gl.uniform1f(u.t, now / 1000);
    gl.uniform2f(u.m, mouse.x, mouse.y);
    gl.uniform1f(u.energy, energy);
    gl.uniform1f(u.dimv, dimv);
    gl.uniform1f(u.scroll, scroll);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
  let raf = 0;
  function loop(now) { draw(now); raf = requestAnimationFrame(loop); }
  addEventListener("resize", size);
  size();
  if (reduce) draw(0);
  else raf = requestAnimationFrame(loop);
  document.addEventListener("visibilitychange", () => {
    if (reduce) return;
    if (document.hidden) { cancelAnimationFrame(raf); raf = 0; }
    else if (!raf) raf = requestAnimationFrame(loop);
  });
  requestAnimationFrame(() => canvas.classList.add("is-on"));
  window.cxFluid = {
    pulse(x) { energyT = Math.min(1, energyT + (x || 0.8)); },
    dim(x) { dimT = Math.max(0, Math.min(1, x)); },
  };
})();
