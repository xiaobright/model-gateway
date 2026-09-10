/* 单画布的液体表面。每个可见模型单独裁剪绘制，文字和交互始终留在 DOM。
   表面用椭球距离场、平滑融合、折射及摄影棚灯带；不引入前端构建工具。 */
const VERTEX = 'attribute vec2 aPosition; varying vec2 uv; void main(){uv=aPosition*.5+.5;gl_Position=vec4(aPosition,0.,1.);}';
const FRAGMENT = [
  'precision highp float;',
  'varying vec2 uv;',
  'uniform vec2 uSize; uniform vec2 uPointer;',
  'uniform vec4 uShapes[24]; uniform float uFlags[24]; uniform int uCount;',
  'uniform vec3 uBase; uniform vec3 uTint;',
  'uniform float uTime; uniform float uDark; uniform float uMotion; uniform float uPulse;',
  'uniform vec4 uLink; uniform float uLinkOn;',
  'float sq(float x){return x*x;}',
  'float merge(float a,float b,float k){float h=clamp(.5+.5*(b-a)/k,0.,1.);return mix(b,a,h)-k*h*(1.-h);}',
  'float sdEllipse(vec3 p,vec3 r){float a=length(p/r);float b=length(p/(r*r));return a*(a-1.)/max(b,.00001);}',
  'float segment(vec2 p,vec2 a,vec2 b){vec2 v=b-a;float t=clamp(dot(p-a,v)/max(dot(v,v),.00001),0.,1.);return length(p-a-v*t);}',
  'float field(vec3 p){',
  '  float distance=10.;',
  '  for(int i=0;i<24;i++){if(i<uCount){',
  '    vec4 s=uShapes[i]; vec3 q=p-vec3(s.xy,0.);',
  '    float waving=uFlags[i]>.5?0.:uMotion;',
  '    q.x+=sin(q.y*14.+uTime*.65+float(i))*.0035*waving;',
  '    q.y+=sin(q.x*13.-uTime*.55+float(i))*.004*waving;',
  '    if(i==0)q.xy+=normalize(q.xy+vec2(.0001))*sin(length(q.xy)*50.-uTime*7.)*.008*uPulse;',
  '    float d=sdEllipse(q,vec3(s.z,s.w,s.w*.65));',
  '    distance=merge(distance,d,.032);',
  '  }}',
  '  if(uLinkOn>.001){',
  '    vec2 a=uLink.xy; vec2 b=uLink.zw; vec2 v=b-a;',
  '    float t=clamp(dot(p.xy-a,v)/max(dot(v,v),.00001),0.,1.);',
  '    vec2 line=mix(a,b,t); line.y+=sin(t*3.14159265)*.07;',
  '    float tube=length(vec3(p.xy-line,p.z))-(.009+.007*sin(t*3.14159265))*uLinkOn;',
  '    distance=merge(distance,tube,.03);',
  '  }',
  '  return distance;',
  '}',
  'vec3 normalAt(vec3 p){vec2 e=vec2(.001,0.);return normalize(vec3(field(p+e.xyy)-field(p-e.xyy),field(p+e.yxy)-field(p-e.yxy),field(p+e.yyx)-field(p-e.yyx)));}',
  'vec3 environment(vec3 d){',
  '  float sky=smoothstep(-.25,.75,d.y);',
  '  vec3 col=mix(vec3(.009,.025,.022),vec3(.30,.49,.40),sky);',
  '  float panel=exp(-sq((d.x+.52+uPointer.x*.18)/.38)-sq((d.y-.78-uPointer.y*.10)/.075));',
  '  float edge=exp(-sq((d.x-.91)/.017)-sq((d.y-.18)/.44));',
  '  float strip=exp(-sq((d.y+.91)/.027)-sq((d.x+.05)/.63));',
  '  float soft=exp(-sq((d.x+.67)/.20)-sq((d.y+.33)/.40));',
  '  col+=vec3(.94,1.,.96)*panel*5.2;',
  '  col+=vec3(.76,.99,.94)*edge*3.1;',
  '  col+=vec3(.89,1.,.69)*strip*1.2;',
  '  col+=uTint*soft*.40;',
  '  return col;',
  '}',
  'void main(){',
  '  vec2 p=(uv-.5)*vec2(uSize.x/uSize.y,1.)*2.;',
  '  float bound=10.; float shadow=0.;',
  '  for(int i=0;i<24;i++){if(i<uCount){',
  '    vec2 d=(p-uShapes[i].xy)/uShapes[i].zw;',
  '    bound=min(bound,(length(d)-1.)*min(uShapes[i].z,uShapes[i].w));',
  '    vec2 sh=(p-uShapes[i].xy+vec2(0.,.025))/uShapes[i].zw;',
  '    shadow+=exp(-dot(sh,sh)*1.4)*.11;',
  '  }}',
  '  if(uLinkOn>.001)bound=min(bound,segment(p,uLink.xy,uLink.zw)-.105);',
  '  vec4 empty=vec4(mix(vec3(.21,.31,.25),vec3(.004,.01,.006),uDark),min(shadow,.20)*(1.-uDark*.35));',
  '  if(bound>.035){gl_FragColor=empty;return;}',
  '  vec3 ro=vec3(p,2.0); vec3 ray=vec3(0.,0.,-1.); vec3 hit=ro; float travel=0.; float sd=1.;',
  '  for(int i=0;i<48;i++){hit=ro+ray*travel;sd=field(hit);if(sd<.0007||travel>3.)break;travel+=max(sd*.82,.0004);}',
  '  if(travel>3.||sd>.003){gl_FragColor=empty;return;}',
  '  vec3 n=normalAt(hit); float face=clamp(n.z,0.,1.); float fresnel=pow(1.-face,4.);',
  '  vec3 reflection=environment(reflect(ray,n));',
  '  vec3 glass=mix(uBase*vec3(.91,1.035,.98),vec3(.025,.12,.09),uDark);',
  '  vec2 grid=abs(fract((p-n.xy*.08)*8.)-.5);',
  '  glass+=max(smoothstep(.484,.499,grid.x),smoothstep(.484,.499,grid.y))*mix(-.017,.024,uDark);',
  '  float reflectionWeight=mix(.07,.33,uDark)+pow(1.-face,2.)*.56;',
  '  vec3 col=mix(glass,reflection,reflectionWeight);',
  '  float spec=pow(max(dot(n,normalize(vec3(-.66,.94,.60))),0.),120.);',
  '  col+=vec3(.91,1.,.94)*spec*.90;',
  '  col+=uTint*pow(1.-face,3.)*.10;',
  '  col=pow(max(vec3(0.),1.-exp(-col*mix(2.0,1.5,uDark))),vec3(.88));',
  '  for(int i=0;i<24;i++){if(i<uCount&&uFlags[i]>.5){',
  '    vec3 q=hit-vec3(uShapes[i].xy,0.);',
  '    if(length(q/vec3(uShapes[i].zw,uShapes[i].w*.65))<1.12){',
  '      vec3 frost=mix(vec3(.71,.80,.83),vec3(.26,.40,.43),uDark);',
  '      if(uFlags[i]>1.5)frost=mix(vec3(.69,.73,.68),vec3(.25,.34,.28),uDark);',
  '      col=mix(col,frost,.64)+sin(hit.x*190.)*sin(hit.y*202.)*.007;',
  '    }',
  '  }}',
  '  gl_FragColor=vec4(clamp(col,0.,1.),1.);',
  '}',
].join('\n');

export function createLiquidSurface(canvas, onLost) {
  let gl;
  try { gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: false, antialias: false, depth: false, powerPreference: 'high-performance' }); }
  catch { return null; }
  if (!gl) return null;
  const shaders = [];
  const compile = (type, source) => {
    const shader = gl.createShader(type);
    shaders.push(shader);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
    return shader;
  };
  const program = gl.createProgram();
  try {
    gl.attachShader(program, compile(gl.VERTEX_SHADER, VERTEX));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAGMENT));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
  } catch (error) {
    console.warn('液体表面不可用，使用玻璃材质回退', error.message);
    gl.deleteProgram(program);
    return null;
  } finally {
    shaders.forEach((shader) => gl.deleteShader(shader));
  }
  gl.useProgram(program);
  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,1,1]), gl.STATIC_DRAW);
  const location = gl.getAttribLocation(program, 'aPosition');
  gl.enableVertexAttribArray(location);
  gl.vertexAttribPointer(location, 2, gl.FLOAT, false, 0, 0);
  const uniform = {};
  for (const name of ['uSize','uPointer','uShapes[0]','uFlags[0]','uCount','uBase','uTint','uTime','uDark','uMotion','uPulse','uLink','uLinkOn']) {
    uniform[name] = gl.getUniformLocation(program, name);
  }
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  const shapes = new Float32Array(96);
  const flags = new Float32Array(24);
  let width = 1, height = 1, dpr = 1;
  let base = [.90,.94,.91];
  let dark = 0;
  let lost = false;
  function theme() {
    const probe = document.createElement('span');
    probe.style.color = 'var(--liq-field)';
    probe.hidden = true;
    canvas.parentElement.append(probe);
    const parts = getComputedStyle(probe).color.match(/[\d.]+/g);
    probe.remove();
    if (parts) base = parts.slice(0, 3).map((v) => Number(v) / 255);
    dark = base.reduce((a,b) => a+b,0) < 1.5 ? 1 : 0;
  }
  function resize(w, h) {
    width = Math.max(1,w); height = Math.max(1,h);
    dpr = Math.min(devicePixelRatio || 1, 1.4);
    const pw = Math.round(width * dpr), ph = Math.round(height * dpr);
    if (canvas.width !== pw || canvas.height !== ph) { canvas.width = pw; canvas.height = ph; }
    canvas.style.height = h + 'px';
    gl.viewport(0,0,canvas.width,canvas.height);
  }
  function draw(items, { scrollTop = 0, time = 0, motion = 1, pointer = { x: .5, y: .5 }, extra = [] } = {}) {
    if (lost) return;
    gl.disable(gl.SCISSOR_TEST);
    gl.clearColor(0,0,0,0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(program);
    gl.uniform2f(uniform.uSize,width,height);
    gl.uniform2f(uniform.uPointer,pointer.x-.5,.5-pointer.y);
    gl.uniform3fv(uniform.uBase,base);
    gl.uniform1f(uniform.uDark,dark);
    gl.uniform1f(uniform.uTime,time);
    gl.uniform1f(uniform.uMotion,motion);
    const scale = 2 / height;
    const unit = (x,y) => [(x-width/2)*scale,(height/2-y+scrollTop)*scale];
    const drawGroup = (item, added = []) => {
      shapes.fill(0); flags.fill(0);
      let count = 0;
      function shape(s, flag = 0) {
        if (count >= 24) return;
        const [x,y] = unit(s.x,s.y);
        shapes.set([x,y,Math.max(1,s.rx)*scale,Math.max(1,s.ry)*scale],count*4);
        flags[count] = flag;
        count++;
      }
      if (item) {
        shape(item.core);
        added.forEach((s) => shape(s));
        for (const candidate of item.drops) {
          if (candidate.y + candidate.ry < scrollTop - 20 || candidate.y - candidate.ry > scrollTop + height + 20) continue;
          shape(candidate,candidate.flag);
        }
        for (const bubble of item.particles || []) shape(bubble);
      }
      else added.forEach((s) => shape(s));
      if (!count) return;
      const active = item?.active;
      const from = active ? unit(active.x,active.y) : [0,0];
      const to = item ? unit(item.core.x,item.core.y) : [0,0];
      gl.uniform4fv(uniform['uShapes[0]'],shapes);
      gl.uniform1fv(uniform['uFlags[0]'],flags);
      gl.uniform1i(uniform.uCount,count);
      gl.uniform3fv(uniform.uTint,item?.protocol === 'anthropic' ? [.85,.83,1.] : [.76,.99,.80]);
      gl.uniform1f(uniform.uPulse,item?.pulse || 0);
      gl.uniform4f(uniform.uLink,from[0],from[1],to[0],to[1]);
      gl.uniform1f(uniform.uLinkOn,active ? 1 : 0);
      gl.drawArrays(gl.TRIANGLE_STRIP,0,4);
    };
    // The dragged drop joins the same distance field as the nearby model. An
    // overlay pass alone cannot form a liquid neck between two separate draws.
    const owners = new Map();
    const floating = [];
    for (const drop of extra) {
      const owner = items.find((item) => drop.x >= item.x - 8 && drop.x <= item.x + item.width + 8
        && drop.y >= item.y - 8 && drop.y <= item.y + item.height + 8);
      if (owner) {
        if (!owners.has(owner)) owners.set(owner,[]);
        owners.get(owner).push(drop);
      } else floating.push(drop);
    }
    gl.enable(gl.SCISSOR_TEST);
    for (const item of items) {
      const additions = owners.get(item) || [];
      const top = Math.max(0,Math.min(item.y-15,...additions.map((s) => s.y-s.ry-12))-scrollTop);
      const bottom = Math.min(height,Math.max(item.y+item.height+15,...additions.map((s) => s.y+s.ry+12))-scrollTop);
      if (bottom <= top) continue;
      const left = Math.max(0,Math.min(item.x-14,...additions.map((s) => s.x-s.rx-12)));
      const right = Math.min(width,Math.max(item.x+item.width+14,...additions.map((s) => s.x+s.rx+12)));
      gl.scissor(Math.floor(left*dpr),Math.floor((height-bottom)*dpr),Math.ceil((right-left)*dpr),Math.ceil((bottom-top)*dpr));
      drawGroup(item,additions);
    }
    gl.disable(gl.SCISSOR_TEST);
    if (floating.length) drawGroup(null,floating);
  }
  canvas.addEventListener('webglcontextlost', (event) => {
    event.preventDefault();
    lost = true;
    canvas.style.visibility = 'hidden';
    onLost?.();
  });
  theme();
  return { resize, draw, theme };
}
