"use strict";
/* ================================================================= *
 *  MINDPRINT v2 — versatile deterministic agent-avatar engine        *
 *  any-color palettes · rich trait genome · rare rolls               *
 * ================================================================= */

function cyrb128(str){
  let h1=1779033703,h2=3144134277,h3=1013904242,h4=2773480762;
  for(let i=0,k;i<str.length;i++){k=str.charCodeAt(i);
    h1=h2^Math.imul(h1^k,597399067);h2=h3^Math.imul(h2^k,2869860233);
    h3=h4^Math.imul(h3^k,951274213);h4=h1^Math.imul(h4^k,2716044179);}
  h1=Math.imul(h3^(h1>>>18),597399067);h2=Math.imul(h4^(h2>>>22),2869860233);
  h3=Math.imul(h1^(h3>>>17),951274213);h4=Math.imul(h2^(h4>>>19),2716044179);
  return [(h1^h2^h3^h4)>>>0,(h2^h1)>>>0,(h3^h1)>>>0,(h4^h1)>>>0];
}
function sfc32(a,b,c,d){return function(){a|=0;b|=0;c|=0;d|=0;
  let t=(a+b|0)+d|0;d=d+1|0;a=b^b>>>9;b=c+(c<<3)|0;c=c<<21|c>>>11;c=c+t|0;
  return (t>>>0)/4294967296;};}

const clamp=(x,a,b)=>x<a?a:x>b?b:x;
const lerp=(a,b,t)=>a+(b-a)*t;
const SQ=x=>x*x;
const smooth=(e0,e1,x)=>{let t=clamp((x-e0)/(e1-e0),0,1);return t*t*(3-2*t);};
const gexp=x=>Math.exp(-x);

/* HSL(0-360,0-1,0-1) -> [r,g,b] 0-255. Powers "any color" + palette modes. */
function hsl2rgb(h,s,l){
  h=((h%360)+360)%360/360;
  const q=l<0.5?l*(1+s):l+s-l*s, p=2*l-q;
  const hk=t=>{t=(t%1+1)%1;
    if(t<1/6)return p+(q-p)*6*t; if(t<1/2)return q;
    if(t<2/3)return p+(q-p)*(2/3-t)*6; return p;};
  return [hk(h+1/3)*255,hk(h)*255,hk(h-1/3)*255];
}
function hex2hs(hex){
  const n=parseInt(hex.slice(1),16), r=(n>>16&255)/255,g=(n>>8&255)/255,b=(n&255)/255;
  const mx=Math.max(r,g,b),mn=Math.min(r,g,b),d=mx-mn; let h=0;
  if(d){ if(mx===r)h=((g-b)/d)%6; else if(mx===g)h=(b-r)/d+2; else h=(r-g)/d+4; h*=60; }
  const l=(mx+mn)/2, s=d===0?0:d/(1-Math.abs(2*l-1));
  return {h:(h+360)%360, s:clamp(s,0.35,1)};
}

/* weighted pick from [[value,weight],...] */
function pick(r,table){ let tot=0; for(const e of table)tot+=e[1]; let x=r()*tot;
  for(const e of table){ if((x-=e[1])<0)return e[0]; } return table[table.length-1][0]; }

const SHAPES=["dot","square","scanline"];
const MODES=["solid","neon","mono","duotone","holo"];

/* ---- tiered emission (the NFT genome) ----
   A top-level tier roll guarantees BASIC (little/no rare parts) >= 66% of
   all emissions; exotic traits only appear in the rarer tiers. */
const TIERS=[["basic",68],["uncommon",21],["rare",8],["legendary",3]];
const TRAITS={
  basic:{ // round/simple only — no exotic anything
    eyes :[["round",70],["square",30]],
    brow :[["none",82],["raised",18]],
    mouth:[["smile",42],["grin",26],["calm",32]],
    ant  :[["none",54],["single",46]],
    aura :[["none",100]],
    mode :[["solid",88],["neon",12]],
  },
  uncommon:{ // one gentle step out
    eyes :[["round",40],["square",24],["sleepy",16],["visor",12],["wink",8]],
    brow :[["none",50],["raised",26],["worried",24]],
    mouth:[["smile",30],["grin",22],["calm",18],["open",18],["o",12]],
    ant  :[["none",24],["single",40],["double",36]],
    aura :[["none",64],["blush",36]],
    mode :[["solid",70],["neon",18],["mono",12]],
  },
  rare:{
    eyes :[["visor",18],["wink",14],["star",22],["heart",18],["sleepy",14],["square",14]],
    brow :[["none",40],["worried",30],["angry",30]],
    mouth:[["open",20],["o",16],["cat",22],["tongue",18],["zigzag",24]],
    ant  :[["single",18],["double",26],["heart",22],["spiral",20],["sideways",14]],
    aura :[["blush",22],["sparkles",30],["halo",30],["bits",18]],
    mode :[["solid",40],["neon",22],["duotone",22],["mono",16]],
  },
  legendary:{ // the wildest rolls
    eyes :[["cyclops",34],["three",30],["star",18],["heart",18]],
    brow :[["angry",44],["worried",30],["none",26]],
    mouth:[["cat",24],["zigzag",24],["open",22],["tongue",16],["grin",14]],
    ant  :[["double",30],["heart",26],["spiral",26],["sideways",18]],
    aura :[["halo",34],["thirdeye",30],["sparkles",22],["bits",14]],
    mode :[["holo",40],["duotone",30],["neon",18],["solid",12]],
  },
};

class Mindprint{
  constructor(seedStr){ this.override={}; this.N=0; this.setSeed(seedStr); }

  setSeed(seedStr){
    this.seedStr=seedStr;
    const s=cyrb128(seedStr||" ");
    this.hex="0x"+((s[0]>>>0).toString(16).toUpperCase().padStart(8,"0")).slice(0,4);
    this.rng=sfc32(s[0],s[1],s[2],s[3]);
    this.noiseSeed=s[1]>>>0;
    this._genParams(); this.N=0;
  }

  _genParams(){
    const r=this.rng, p={};
    // rarity tier first — guarantees basic >= 66% of emissions
    const tier=pick(r,TIERS); p.tier=tier; const TT=TRAITS[tier];
    // head
    const headType=Math.floor(r()*3);            // 0 orb 1 android 2 tall
    p.headExp=[2.0,4.0,2.4][headType];
    p.headType=headType;
    p.headRx=0.33+r()*0.05;
    p.headRy=headType===2?0.40+r()*0.04:0.34+r()*0.05;
    // eye placement
    p.eyeSep=0.150+r()*0.055;
    p.eyeY=-0.02+r()*0.09;
    p.eyeR=0.055+r()*0.030;
    p.eyeBright=0.9+r()*0.35;
    p.eyeStyle=pick(r,TT.eyes);
    if(p.eyeStyle==="cyclops"){p.eyeR*=1.7;p.eyeSep=0;}
    p.winkSide=r()<0.5?-1:1;
    // brows
    p.brow=pick(r,TT.brow);
    // cheeks
    p.cheekX=p.eyeSep+0.012+r()*0.02;
    p.cheekY=0.13+r()*0.05;
    p.cheekStr=0.05+r()*0.08;
    // mouth
    p.mouth=pick(r,TT.mouth);
    p.mouthY=0.15+r()*0.05;
    p.mouthW=0.095+r()*0.05;
    p.smile=0.030+r()*0.055;
    p.mouthBright=0.55+r()*0.28;
    // antenna
    p.ant=pick(r,TT.ant);
    p.antX=(r()-0.5)*0.10;
    p.antTop=-(p.headRy)-0.03-r()*0.03;
    p.antSpread=0.10+r()*0.05;
    p.asymX=(r()-0.5)*0.05;
    // aura / accessory
    p.aura=pick(r,TT.aura);
    // life
    this.blinkPhase=r()*8; this.blinkEvery=3.4+r()*3.2;
    // color (auto from seed): full-spectrum hue
    p.hue=Math.floor(r()*360);
    p.sat=0.62+r()*0.34;
    p.mode=pick(r,TT.mode);
    // mesh dials
    p.densAuto=Math.round(lerp(46,66,r())/2)*2;
    p.grainAuto=0.12+r()*0.14;
    p.shapeIdx=Math.floor(r()*SHAPES.length);
    // voice signature — seeded per agent, REMEMBERED into the config so the
    // upcoming voice-interface app speaks in this agent's own voice.
    p.voice={pitch:+clamp(0.7+r()*0.9,0,2).toFixed(2),
             rate:+clamp(0.86+r()*0.44,0.5,1.6).toFixed(2),
             timbre:+r().toFixed(2)};
    this.p=p;
    // sparkle positions (seeded, fixed)
    p.spark=[]; for(let k=0;k<5;k++)p.spark.push([(r()-0.5)*0.9,(r()-0.5)*0.9,0.02+r()*0.02]);
    p.bits=[]; for(let k=0;k<9;k++)p.bits.push([(r()-0.5)*1.0,(r()-0.5)*1.0,0.4+r()*0.6]);
  }

  cfg(){
    const p=this.p,o=this.override;
    let hue=p.hue,sat=p.sat;
    if(o.color){ const hs=hex2hs(o.color); hue=hs.h; sat=hs.s; }
    return {
      hue, sat,
      mode:o.mode!=null?o.mode:p.mode,
      N:o.dens!=null?o.dens:p.densAuto,
      grain:o.grain!=null?o.grain:p.grainAuto,
      shape:SHAPES[o.shape!=null?o.shape:p.shapeIdx],
    };
  }

  voiceCfg(){ const v=this.p.voice, o=this.override.voice||{};
    return {pitch:o.pitch!=null?o.pitch:v.pitch,
            rate:o.rate!=null?o.rate:v.rate,
            timbre:o.timbre!=null?o.timbre:v.timbre}; }

  traitList(){ const p=this.p,c=this.cfg();
    return {tier:p.tier,eyes:p.eyeStyle,brow:p.brow,mouth:p.mouth,antenna:p.ant,aura:p.aura,
            head:["orb","android","tall"][p.headType],mode:c.mode,voice:this.voiceCfg()}; }

  _n2(ix,iy){let h=(Math.imul(ix,374761393)+Math.imul(iy,668265263)+this.noiseSeed)|0;
    h=Math.imul(h^(h>>>13),1274126177);return ((h^(h>>>16))>>>0)/4294967296;}
  _vnoise(x,y){const xi=Math.floor(x),yi=Math.floor(y),xf=x-xi,yf=y-yi;
    const u=xf*xf*(3-2*xf),v=yf*yf*(3-2*yf);
    const a=this._n2(xi,yi),b=this._n2(xi+1,yi),c=this._n2(xi+1,yi+1),d=this._n2(xi,yi+1);
    return lerp(lerp(a,b,u),lerp(d,c,u),v);}
  _fbm(x,y){let f=0,amp=0.5,frq=1,norm=0;
    for(let o=0;o<3;o++){f+=amp*this._vnoise(x*frq,y*frq);norm+=amp;amp*=0.55;frq*=2.1;}return f/norm;}

  build(){const c=this.cfg(),N=c.N;
    if(this.N===N&&this._grain)return c; this.N=N;
    const grain=new Float32Array(N*N),phase=new Float32Array(N*N),gs=6.0;
    for(let j=0;j<N;j++){const v=(j+0.5)/N;
      for(let i=0;i<N;i++){const idx=j*N+i,u=(i+0.5)/N;
        grain[idx]=this._fbm(u*gs,v*gs)*2-1; phase[idx]=this._n2(i*7+3,j*13+5)*6.283;}}
    this._grain=grain;this._phase=phase;return c;}

  // build eye descriptors for current params (positions + styles)
  _eyes(){const p=this.p,st=p.eyeStyle,eyes=[];
    if(st==="cyclops"){eyes.push({x:0,y:p.eyeY,R:p.eyeR,style:"round",blink:true,B:p.eyeBright});return {eyes,style:st};}
    const mk=(sx,style)=>({x:sx*p.eyeSep,y:p.eyeY,R:p.eyeR,style,blink:style!=="wink",B:p.eyeBright});
    if(st==="wink"){eyes.push(mk(-1,p.winkSide<0?"wink":"round"));eyes.push(mk(1,p.winkSide<0?"round":"wink"));}
    else if(st==="visor"){/* drawn as bar, no per-eye */}
    else{eyes.push(mk(-1,st));eyes.push(mk(1,st));}
    if(st==="three")eyes.push({x:0,y:p.eyeY-0.17,R:p.eyeR*0.8,style:"round",blink:true,B:p.eyeBright});
    return {eyes,style:st};}

  render(ctx,size,t,amp,opts){
    opts=opts||{}; const still=opts.still; const c=this.build();
    const N=this.N,cell=size/N,p=this.p;
    const grain=this._grain,phase=this._phase,grainAmt=c.grain;
    const hue=c.hue,sat=c.sat,mode=c.mode,shape=c.shape;
    const breath=still?1:(1+0.035*Math.sin(t*1.05));
    const shimmer=still?0:0.05;
    let eyeOpen=1;
    if(!still){const bt=((t+this.blinkPhase)%this.blinkEvery);
      if(bt<0.15){const d=bt/0.15;eyeOpen=0.10+0.90*Math.abs(2*d-1);}}
    const glow=1+amp*0.3;
    const talk=clamp(Math.max(amp,p.mouth==="open"?0.16:(p.mouth==="tongue"?0.10:0.02)),0,1);
    const spark=still?1:(0.85+0.22*Math.sin(t*2.2+this.blinkPhase*3));
    const rx=p.headRx,ry=p.headRy,nexp=p.headExp;
    const asym=p.asymX*0.1, bw=1/breath;
    const E=this._eyes(), eyes=E.eyes, eStyle=E.style;

    ctx.globalCompositeOperation="source-over";
    ctx.fillStyle="#000";ctx.fillRect(0,0,size,size);
    // colored aura wash
    const midc=hsl2rgb(hue,sat*0.9,0.5);
    const ag=ctx.createRadialGradient(size/2,size*0.46,0,size/2,size*0.46,size*0.52);
    ag.addColorStop(0,`rgba(${midc[0]|0},${midc[1]|0},${midc[2]|0},0.09)`);
    ag.addColorStop(1,"rgba(0,0,0,0)");
    ctx.fillStyle=ag;ctx.fillRect(0,0,size,size);
    ctx.globalCompositeOperation="lighter";

    for(let j=0;j<N;j++){
      const Y=((j+0.5)/N-0.5)*bw;
      for(let i=0;i<N;i++){
        const idx=j*N+i,X=((i+0.5)/N-0.5)*bw;
        const dh=Math.pow(Math.pow(Math.abs(X/rx),nexp)+Math.pow(Math.abs(Y/ry),nexp),1/nexp);
        const mask=smooth(1.03,0.80,dh);
        let lum=mask*(0.24+0.34*(1-clamp(dh,0,1)));
        lum+=0.15*gexp(SQ((dh-1.0)/0.13));
        lum+=grain[idx]*grainAmt*(0.4*mask+0.05);
        if(!still)lum*=1+shimmer*Math.sin(t*3.0+phase[idx]);
        const ax=Math.abs(X)-Math.sign(X)*asym;

        // ---- eyes ----
        if(eStyle==="visor"){
          if(Math.abs(Y-p.eyeY)<0.045 && Math.abs(X)<rx*0.72){
            lum+=p.eyeBright*1.0*gexp(SQ((Y-p.eyeY)/0.026))*mask;
            // scanning bright dot
            const sx=(still?0.3:0.6*Math.sin(t*1.7))*rx*0.6;
            lum+=0.7*gexp(SQ((X-sx)/0.04)+SQ((Y-p.eyeY)/0.03))*mask;
          }
        } else {
          for(const e of eyes){
            const edx=X-e.x, oy=e.R*(e.blink?eyeOpen:1)+1e-4, edy=Y-e.y;
            let I=0;
            if(e.style==="wink"){ // a bright upward arc ^
              const cv=e.y-0.012-0.05*(1-SQ(clamp(edx/(e.R*1.4),-1,1)));
              if(Math.abs(edx)<e.R*1.5) I=e.B*0.9*gexp(SQ((Y-cv)/0.012));
            } else if(e.style==="square"){
              const bx=Math.max(0,Math.abs(edx)-e.R*0.55)/(e.R*0.5);
              const by=Math.max(0,Math.abs(edy)-oy*0.55)/(oy*0.5);
              const e2=bx*bx+by*by; I=e.B*(gexp(e2*2.1)+0.85*gexp(e2*6.5));
            } else if(e.style==="sleepy"){
              const e2=SQ(edx/e.R)+SQ(edy/(oy*0.5)); I=e.B*(gexp(e2*2.4)+0.7*gexp(e2*7));
            } else if(e.style==="star"){
              const e2=SQ(edx/e.R)+SQ(edy/oy); const ang=Math.atan2(edy,edx);
              const star=0.5+0.5*Math.cos(5*ang); I=e.B*(gexp(e2*1.6)*(0.35+0.65*star*star)+0.5*gexp(e2*9));
            } else if(e.style==="heart"){
              const R=e.R*1.15;
              const hl=gexp((SQ((edx+0.34*R)/(0.52*R))+SQ((edy+0.30*R)/(0.52*R))));
              const hr=gexp((SQ((edx-0.34*R)/(0.52*R))+SQ((edy+0.30*R)/(0.52*R))));
              const hb=gexp((SQ(edx/(0.62*R))+SQ((edy-0.52*R)/(0.72*R))));
              I=e.B*clamp(hl+hr+hb,0,1.25);
            } else { // round
              const e2=SQ(edx/e.R)+SQ(edy/oy);
              I=e.B*(gexp(e2*2.1)+0.85*gexp(e2*6.5));
              I+=spark*0.55*gexp(SQ((edx-e.R*0.30)/(e.R*0.32))+SQ((edy+oy*0.32)/(oy*0.32)));
            }
            lum+=I*mask;
          }
        }

        // ---- brows ----
        if(p.brow!=="none" && eStyle!=="visor"){
          const bs=p.brow==="angry"?1:(p.brow==="worried"?-1:0);
          const by0=p.eyeY-p.eyeR-0.055;
          for(const sx of [-1,1]){
            const bx=X-sx*p.eyeSep, tilt=bs*sx*2.6;
            const yline=by0 + tilt*bx + (p.brow==="raised"?-0.02:0);
            if(Math.abs(bx)<p.eyeR*1.3) lum+=0.5*gexp(SQ((Y-yline)/0.011))*mask;
          }
        }

        // ---- cheeks / blush ----
        const cheek=(p.aura==="blush")?p.cheekStr*2.4:p.cheekStr;
        lum+=cheek*gexp(SQ((ax-p.cheekX)/0.10)+SQ((Y-p.cheekY)/0.08))*mask;

        // ---- mouth ----
        const m=p.mouth;
        if(m==="o"){
          const d=Math.sqrt(SQ(ax/1.0)+SQ((Y-p.mouthY-0.01)/1.0));
          lum+=p.mouthBright*gexp(SQ((d-0.055)/0.02))*mask;
        } else if(m==="cat"){ // :3  -> w shape
          if(ax<p.mouthW*1.2){const w=p.mouthY+0.02*Math.cos(ax/p.mouthW*6.283*1.5);
            lum+=p.mouthBright*gexp(SQ((Y-w)/0.014))*mask;}
        } else if(m==="zigzag"){ // robot teeth
          if(ax<p.mouthW*1.1){const zz=p.mouthY+0.018*(2*Math.abs(((ax/p.mouthW*4)%1)-0.5)-0.5);
            lum+=p.mouthBright*0.9*gexp(SQ((Y-zz)/0.012))*mask;}
        } else if(m==="tongue"){
          const curveY=p.mouthY+p.smile*(1-SQ(clamp(ax/p.mouthW,0,1)));
          if(ax<p.mouthW*1.15)lum+=p.mouthBright*gexp(SQ((Y-curveY)/0.016))*mask;
          lum+=0.6*gexp(SQ(ax/(p.mouthW*0.5))+SQ((Y-p.mouthY-0.05)/0.03))*mask; // tongue blob
        } else { // smile / grin / calm / open
          if(ax<p.mouthW*1.15){
            const curveY=p.mouthY+p.smile*(1-SQ(ax/p.mouthW));
            lum+=p.mouthBright*gexp(SQ((Y-curveY)/(0.016+0.012*talk)))*mask*(0.65+0.35*(1-talk));
          }
          if(talk>0.02)lum+=talk*0.85*gexp(SQ(ax/(p.mouthW*0.72))+SQ((Y-p.mouthY-0.004)/(0.014+talk*0.05)))*mask;
        }

        // ---- antenna(s) ----
        if(p.ant!=="none"){
          const drawAnt=(axpos,tip)=>{
            if(tip==="heart"){
              const R=0.03;
              lum+=0.7*gexp(SQ((X-axpos+0.013)/(0.6*R))+SQ((Y-p.antTop+0.008)/(0.6*R)));
              lum+=0.7*gexp(SQ((X-axpos-0.013)/(0.6*R))+SQ((Y-p.antTop+0.008)/(0.6*R)));
              lum+=0.7*gexp(SQ((X-axpos)/(0.8*R))+SQ((Y-p.antTop-0.02)/(0.9*R)));
            } else if(tip==="spiral"){
              for(let k=0;k<7;k++){const a=k*0.9,rr=0.006+k*0.004;
                const sxp=axpos+Math.cos(a)*rr, syp=p.antTop+Math.sin(a)*rr;
                lum+=0.5*gexp(SQ((X-sxp)/0.014)+SQ((Y-syp)/0.014));}
            } else {
              lum+=0.7*gexp(SQ((X-axpos)/0.017)+SQ((Y-p.antTop)/0.02));
            }
            // stalk
            if(X>axpos-0.008&&X<axpos+0.008&&Y<(-ry*0.72)&&Y>p.antTop)lum+=0.22;
          };
          if(p.ant==="single")drawAnt(p.antX,"dot");
          else if(p.ant==="heart")drawAnt(p.antX,"heart");
          else if(p.ant==="spiral")drawAnt(p.antX,"spiral");
          else if(p.ant==="sideways"){lum+=0.7*gexp(SQ((X-(p.antX+0.14))/0.02)+SQ((Y-p.antTop*0.6)/0.02));
            if(Y>p.antTop*0.6-0.006&&Y<p.antTop*0.6+0.006&&X>p.antX&&X<p.antX+0.14)lum+=0.2;}
          else if(p.ant==="double"){drawAnt(-p.antSpread,"dot");drawAnt(p.antSpread,"dot");}
        }

        // ---- aura extras ----
        if(p.aura==="halo"){
          const r2=Math.sqrt(SQ(X/1.05)+SQ((Y+ry+0.12)/0.5));
          lum+=0.6*gexp(SQ((r2-0.30)/0.05));
        } else if(p.aura==="thirdeye"){
          const e2=SQ(X/(p.eyeR*0.8))+SQ((Y-(p.eyeY-0.16))/(p.eyeR*0.8));
          lum+=0.9*p.eyeBright*(gexp(e2*2.2)+0.8*gexp(e2*7))*mask;
        } else if(p.aura==="sparkles"){
          for(const s of p.spark){const tw=still?1:(0.6+0.4*Math.sin(t*3+s[0]*10));
            lum+=0.6*tw*gexp((SQ((X-s[0])/s[2])+SQ((Y-s[1])/s[2])));}
        } else if(p.aura==="bits"){
          for(const b of p.bits){lum+=0.22*b[2]*gexp((SQ((X-b[0])/0.02)+SQ((Y-b[1])/0.02)));}
        }

        lum*=glow;
        if(lum<=0.05)continue;
        const v01=clamp(lum,0,1);
        // ---- color: any hue, unified, palette modes ----
        let H=hue,S=sat;
        if(mode==="neon")S=Math.min(1,sat*1.35);
        else if(mode==="mono")S=0.06;
        else if(mode==="duotone")H=hue+150*smooth(-0.25,0.30,Y);
        else if(mode==="holo")H=hue+46*Math.sin(dh*3.0+Y*1.5+X*0.6);
        const L=0.05+0.92*Math.pow(v01,0.85);
        const Sc=S*(1-0.68*smooth(0.60,1.0,v01));   // glow to white at peaks
        const col=hsl2rgb(H,Sc,L);
        const cx=(i+0.5)*cell,cy=(j+0.5)*cell,rad=cell*0.5*(0.30+0.85*v01);
        ctx.fillStyle=`rgb(${col[0]|0},${col[1]|0},${col[2]|0})`;
        ctx.globalAlpha=v01;
        if(shape==="square"){const s2=rad*1.7;ctx.fillRect(cx-s2/2,cy-s2/2,s2,s2);}
        else if(shape==="scanline"){const w=cell*(0.35+0.9*v01),h=Math.max(1,cell*0.42);ctx.fillRect(cx-w/2,cy-h/2,w,h);}
        else{ctx.beginPath();ctx.arc(cx,cy,rad,0,6.2832);ctx.fill();}
        if(v01>0.62){ctx.globalAlpha=(v01-0.62)*0.5;ctx.beginPath();ctx.arc(cx,cy,rad*2.3,0,6.2832);ctx.fill();}
      }
    }
    ctx.globalAlpha=1;ctx.globalCompositeOperation="source-over";
  }
}

/* --- dual exposure: classic global (file:// studio/logo) + ES module (webview) --- */
if (typeof window !== "undefined") {
  window.Mindprint = Mindprint; window.PALETTES = (typeof PALETTES!=="undefined"?PALETTES:null);
  window.SHAPES = SHAPES; window.MODES = MODES; window.TIERS = TIERS; window.TRAITS = TRAITS;
  window.cyrb128 = cyrb128; window.sfc32 = sfc32; window.hsl2rgb = hsl2rgb;
}
