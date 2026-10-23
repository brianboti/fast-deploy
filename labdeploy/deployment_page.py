"""Plain-English deployment workspace with evidence-driven progress and progressive detail."""

import datetime
import json
from urllib.parse import urlencode

from . import settings_page as ui
from .deployments import LABELS

STYLE = """
.deployment{--ink:#172e3b;--quiet:#647785;--line:#dfe7eb;--teal:#087f78;color:var(--ink)}

.deployment .d-eyebrow{font-size:11px;letter-spacing:.12em;text-transform:uppercase;font-weight:700;color:var(--quiet)}

.deployment .d-top{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:24px}

.deployment h1{font-size:32px;letter-spacing:-1.1px;margin:6px 0 8px;line-height:1.16}

.deployment h2{font-size:17px;letter-spacing:-.3px;margin:0}
.deployment h3{font-size:14px;margin:0}

.deployment p{margin:6px 0;color:var(--quiet);line-height:1.6}
.deployment .d-caption{font-size:12px;color:var(--quiet)}

.deployment .d-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.deployment .d-tools a,.deployment .d-tools button{white-space:normal}

.deployment .d-live{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:#176c61;font-weight:600}

.deployment .d-dot{width:7px;height:7px;flex:none;border-radius:50%;background:currentColor}

.deployment .d-hero{border:1px solid #ccdfe0;background:linear-gradient(115deg,#f2fbf9,#fff 65%);border-radius:18px;padding:26px;margin-bottom:20px;box-shadow:0 8px 30px #173a4610}

.deployment .d-hero-head{display:flex;align-items:center;gap:22px}
.deployment .d-hero h2{font-size:23px;line-height:1.3;letter-spacing:-.6px}

.deployment .d-ring{width:94px;height:94px;flex:none}
.deployment .d-ring text{font:600 19px system-ui;fill:var(--ink)}

.deployment .d-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;margin-top:24px;padding-top:18px;border-top:1px solid #dce8e6}

.deployment .d-stat strong{display:block;font-size:19px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}

.deployment .d-columns{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(240px,1fr);gap:20px;align-items:start}

.deployment .d-panel{background:white;border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-bottom:18px;min-width:0}

.deployment .d-panel-head{padding:19px 20px;border-bottom:1px solid #edf1f3}
.deployment .d-pad{padding:18px 20px}

.deployment .d-job{border-bottom:1px solid #edf1f3}
.deployment .d-job:last-child{border-bottom:0}

.deployment summary{padding:16px 20px;cursor:pointer;list-style:none;display:flex;gap:10px;align-items:center}

.deployment summary::-webkit-details-marker{display:none}
.deployment summary:after{content:'+';margin-left:auto;color:var(--quiet)}

.deployment details[open]>summary:after{content:'−'}
.deployment summary .d-job-name{font-weight:600;overflow-wrap:anywhere}

.deployment .d-steps{margin:0;padding:0 20px 14px;list-style:none}
.deployment .d-step{display:grid;grid-template-columns:25px minmax(0,1fr) auto;gap:10px;align-items:start;padding:10px 0;position:relative}

.deployment .d-step:not(:last-child):before{content:'';position:absolute;top:36px;bottom:-7px;left:11px;width:1px;background:#dce5e9}

.deployment .d-symbol{width:24px;height:24px;border-radius:50%;display:grid;place-items:center;background:#eef2f5;color:#647785;font-size:12px;flex:none}

.deployment .done{color:#176d58;background:#e5f3ec}
.deployment .running{color:#126da0;background:#e6f1fa}
.deployment .failed{color:#a43439;background:#fdecef}

.deployment .cancelled,.deployment .skipped{background:#f2f1ed;color:#7a7157}
.deployment .d-step-name{overflow-wrap:anywhere;font-size:13px}

.deployment .d-badge{display:inline-flex;align-items:center;gap:6px;border-radius:6px;padding:4px 8px;font-size:11px;font-weight:600;white-space:normal}

.deployment .d-timing{font-size:11px;color:var(--quiet);font-variant-numeric:tabular-nums;padding-top:3px}

.deployment .d-facts{margin:0}
.deployment .d-facts div{padding:9px 0;border-bottom:1px solid #edf1f3}
.deployment .d-facts div:last-child{border:0}

.deployment dt{font-size:11px;color:var(--quiet);margin-bottom:3px}
.deployment dd{margin:0;font-size:13px;overflow-wrap:anywhere}

.deployment .d-notice{border-left:3px solid #c78b25;background:#fff8e9;padding:14px 17px;border-radius:0 8px 8px 0;margin:16px 0;color:#775319}

.deployment .d-notice p{color:inherit}
.deployment .d-error{border-color:#bd4c4c;background:#fff1f1;color:#873e3e}

.deployment .d-checks{display:grid;gap:9px}
.deployment .d-check{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:13px}

.deployment .d-traffic{display:flex;height:8px;border-radius:6px;overflow:hidden;background:#edf2f4;margin:10px 0}
.deployment .d-traffic span{background:#087f78}

.deployment .d-activity{list-style:none;margin:0;padding:0}
.deployment .d-activity li{display:grid;grid-template-columns:62px 1fr;gap:12px;padding:9px 0;border-bottom:1px solid #edf1f3;font-size:12px}

.deployment .d-activity time{color:var(--quiet);font-variant-numeric:tabular-nums}
.deployment .d-empty{padding:28px 20px;color:var(--quiet);text-align:center}

.deployment pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;background:#f5f7f9;border-radius:8px;padding:14px;max-height:420px;overflow:auto}

.deployment .d-run{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:16px;padding:20px;border-bottom:1px solid #edf1f3;text-decoration:none;color:inherit}

.deployment .d-run:hover{background:#f6fafb}
.deployment .d-run strong{font-size:15px;display:block}
.deployment .d-run:last-child{border:0}

.deployment .d-list-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:15px;margin-bottom:22px}
.deployment .d-list-stats>div{padding:18px 20px;background:white;border:1px solid var(--line);border-radius:12px}

.deployment .d-list-stats strong{font-size:26px;display:block;font-weight:600}
.deployment .d-list-stats span{font-size:12px;color:var(--quiet)}

.deployment .d-back{font-size:12px;text-decoration:none;display:inline-block;margin-bottom:12px}
.deployment a{overflow-wrap:anywhere}

.deployment .d-announcement{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}

/* ---- the live view: the board, the track, the spotlight, the visitors, the journey ---- */
.deployment .d-board{border:1px solid #cfe0e0;background:
  radial-gradient(900px 240px at 0% 0%,#e6f7f3 0,#f7fcfb 45%,#fff 70%);border-radius:20px;padding:26px 28px 22px;margin-bottom:18px;
  box-shadow:0 10px 36px #173a4612;position:relative;overflow:hidden}
.deployment .d-board.b-live{border-color:#bfe3d8}
.deployment .d-board.b-now{border-color:#bcd8ee;background:radial-gradient(900px 240px at 0% 0%,#e9f3fc 0,#f7fafd 45%,#fff 70%)}
.deployment .d-board.b-stop{border-color:#f0c4c8;background:radial-gradient(900px 240px at 0% 0%,#fdecee 0,#fff7f7 45%,#fff 70%)}
.deployment .d-board-head{display:flex;align-items:center;gap:26px}
.deployment .d-board h2{font-size:26px;line-height:1.25;letter-spacing:-.7px;margin:6px 0 6px}
.deployment .d-board .d-lead{font-size:15px;color:var(--ink);margin:0}
.deployment .d-board .d-lead strong{font-weight:700}
.deployment .d-board .d-lead-sub{font-size:14px;margin:4px 0 0}
.deployment .d-pill{display:inline-flex;align-items:center;gap:7px;border-radius:999px;padding:5px 11px 5px 9px;font-size:12px;font-weight:700;letter-spacing:.02em}
.deployment .d-pill i{width:8px;height:8px;border-radius:50%;background:currentColor;flex:none;position:relative}
.deployment .d-pill.running{background:#e2effa;color:#145e93}
.deployment .d-pill.running i:after{content:'';position:absolute;inset:-4px;border-radius:50%;border:2px solid currentColor;opacity:0;animation:d-ripple 1.6s ease-out infinite}
.deployment .d-pill.done{background:#dff3ea;color:#176d58}
.deployment .d-pill.failed{background:#fde4e7;color:#a43439}
.deployment .d-pill.skipped{background:#efece4;color:#7a7157}

.deployment .d-ring{width:112px;height:112px;flex:none;filter:drop-shadow(0 6px 14px #087f7822)}
.deployment .d-ring .d-ring-track{stroke:#dbe9e8}
.deployment .d-ring .d-ring-fill{transition:stroke-dasharray .9s cubic-bezier(.3,.7,.2,1)}
.deployment .d-ring text{font:700 22px system-ui;fill:var(--ink)}
.deployment .d-ring .d-ring-sub{font:600 9.5px system-ui;fill:var(--quiet);letter-spacing:.08em}
.deployment .d-board.b-now .d-ring .d-ring-fill{stroke:#2b7fc0}
.deployment .d-board.b-stop .d-ring .d-ring-fill{stroke:#c8484e}

.deployment .d-board .d-stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px;margin-top:22px;padding-top:18px;border-top:1px solid #d9e7e5}
.deployment .d-board .d-stat strong{font-size:22px;letter-spacing:-.4px}

/* the track: every stage as a node on one line, grouped by what the group does */
.deployment .d-track{margin-top:22px;padding-top:18px;border-top:1px solid #d9e7e5}
.deployment .d-track ol{list-style:none;margin:0;padding:0;display:grid;grid-auto-flow:column;grid-auto-columns:1fr}
.deployment .d-track .d-phases{display:grid;grid-auto-flow:column;margin-bottom:10px}
.deployment .d-track .d-phase{font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--quiet);text-align:center;
  padding-bottom:8px;position:relative}
.deployment .d-track .d-phase:after{content:'';position:absolute;left:10px;right:10px;bottom:0;height:3px;border-radius:3px;background:#e4ebee}
.deployment .d-track .d-phase.p-done:after{background:#2f9e78}
.deployment .d-track .d-phase.p-now:after{background:#2b7fc0}
.deployment .d-track .d-phase.p-stop:after{background:#c8484e}
.deployment .d-track .d-phase.p-now{color:#145e93}
.deployment .d-track li{position:relative;text-align:center;padding:4px 2px 0;min-width:0}
.deployment .d-track li:before,.deployment .d-track li:after{content:'';position:absolute;top:19px;height:3px;background:#e4ebee}
.deployment .d-track li:before{left:0;right:50%}
.deployment .d-track li:after{left:50%;right:0}
.deployment .d-track li:first-child:before,.deployment .d-track li:last-child:after{display:none}
.deployment .d-track li.in-done:before,.deployment .d-track li.out-done:after{background:#2f9e78}
.deployment .d-track li.in-stop:before,.deployment .d-track li.out-stop:after{background:#c8484e}
.deployment .d-track li.in-flow:before{background:repeating-linear-gradient(90deg,#2b7fc0 0 8px,#9ccbee 8px 16px);background-size:32px 100%;
  animation:d-flow .8s linear infinite}
.deployment .d-track a{display:flex;flex-direction:column;align-items:center;gap:8px;text-decoration:none;color:inherit;position:relative;z-index:1}
.deployment .d-track a:hover .d-tl{color:var(--ink)}
.deployment .d-tn{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;font-size:12px;font-weight:700;background:#fff;
  border:2px solid #d5dee3;color:#8796a1;position:relative;transition:transform .3s}
.deployment .d-tl{font-size:12px;font-weight:600;color:var(--quiet);line-height:1.2;overflow-wrap:anywhere}
.deployment .d-track .t-done .d-tn,.deployment .d-track .t-skipped .d-tn{background:#2f9e78;border-color:#2f9e78;color:#fff}
.deployment .d-track .t-done .d-tl{color:#176d58}
.deployment .d-track .t-running .d-tn,.deployment .d-track .t-next .d-tn{background:#2b7fc0;border-color:#2b7fc0;color:#fff;animation:d-pulse 1.6s ease-out infinite}
.deployment .d-track .t-running .d-tn:after{content:'';position:absolute;inset:-7px;border-radius:50%;border:2px solid transparent;border-top-color:#2b7fc0;
  border-right-color:#2b7fc080;animation:d-spin 1s linear infinite}
.deployment .d-track .t-running .d-tl,.deployment .d-track .t-next .d-tl{color:#145e93}
.deployment .d-track .t-failed .d-tn,.deployment .d-track .t-cancelled .d-tn{background:#c8484e;border-color:#c8484e;color:#fff}
.deployment .d-track .t-failed .d-tl{color:#a43439}
.deployment .d-track .t-not_run{opacity:.45}

/* the spotlight: the stage happening now, said in full */
.deployment .d-duo{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(280px,1fr);gap:18px;align-items:stretch;margin-bottom:18px}
.deployment .d-duo>.d-panel{margin-bottom:0}
.deployment .d-now-card{border-color:#bcd8ee;background:linear-gradient(160deg,#f3f8fd,#fff 55%);position:relative;overflow:hidden}
.deployment .d-now-card.n-live{border-color:#bfe3d8;background:linear-gradient(160deg,#eef9f5,#fff 55%)}
.deployment .d-now-card.n-stop{border-color:#f0c4c8;background:linear-gradient(160deg,#fff0f1,#fff 55%)}
.deployment .d-now-card.n-wait{border-color:var(--line);background:#fff}
.deployment .d-now-card .d-pad{padding:22px 24px}
.deployment .d-now-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}
.deployment .d-now-card .d-eyebrow{color:#145e93}
.deployment .d-now-card.n-live .d-eyebrow{color:#176d58}
.deployment .d-now-card.n-stop .d-eyebrow{color:#a43439}
.deployment .d-now-card h2{font-size:22px;letter-spacing:-.5px;margin:6px 0 10px;line-height:1.25}
.deployment .d-now-card .d-say{font-size:15px;color:var(--ink);line-height:1.6;margin:0}
.deployment .d-now-clock{text-align:right;flex:none}
.deployment .d-now-clock strong{display:block;font-size:30px;font-weight:700;letter-spacing:-1px;font-variant-numeric:tabular-nums;line-height:1;color:#145e93}
.deployment .d-now-card.n-live .d-now-clock strong{color:#176d58}
.deployment .d-now-card.n-stop .d-now-clock strong{color:#a43439}
.deployment .d-now-clock>span{font-size:11px;color:var(--quiet);text-transform:uppercase;letter-spacing:.08em;font-weight:700}
.deployment .d-why{display:grid;gap:8px;margin:16px 0 0;padding:14px 0 0;border-top:1px dashed #cfdce3}
.deployment .d-why div{display:grid;grid-template-columns:22px minmax(0,1fr);gap:10px;align-items:start;font-size:13.5px;color:#41535f;line-height:1.5}
.deployment .d-why b{display:block;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--quiet);margin-bottom:1px}
.deployment .d-why i{width:22px;height:22px;border-radius:7px;display:grid;place-items:center;font-style:normal;font-size:12px;font-weight:700;margin-top:2px}
.deployment .d-why .w-why i{background:#e2effa;color:#145e93}
.deployment .d-why .w-fail i{background:#fde9d7;color:#9a4b12}
.deployment .d-why .w-done i{background:#eef2f5;color:#8796a1}
.deployment details.d-more{margin-top:14px}
.deployment details.d-more>summary{padding:0;display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:600;color:#145e93;cursor:pointer}
.deployment details.d-more>summary:after{content:'+';margin-left:2px;color:inherit}
.deployment details.d-more[open]>summary:after{content:'−'}
.deployment details.d-more>summary:hover{text-decoration:underline}
.deployment details.d-more .d-why{margin-top:10px}
.deployment .d-links{display:flex;flex-wrap:wrap;gap:4px 20px;align-items:baseline;margin-top:8px}
.deployment .d-links>details{margin-top:0}
.deployment .d-links>details[open]{flex-basis:100%}
.deployment .d-now-card .btn.primary{margin-top:16px}

/* the watch: a bar that fills over the window, under a line that keeps beating while the new version is watched */
.deployment .d-watch{margin-top:16px}
.deployment .d-watch-bar{height:12px;border-radius:7px;background:#dceaf6;overflow:hidden;position:relative}
.deployment .d-watch-bar span{display:block;height:100%;background:linear-gradient(90deg,#2b7fc0,#48a2e0);transition:width 1s linear;border-radius:7px;position:relative}
.deployment .d-watch-bar span:after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,#ffffff66,transparent);
  background-size:200px 100%;animation:d-shine 1.8s linear infinite}
.deployment .d-watch-text{font-size:13px;color:#145e93;margin-top:7px;font-variant-numeric:tabular-nums;font-weight:600}
.deployment .d-beat{display:block;width:100%;height:46px;margin:6px 0 2px}
.deployment .d-beat path{fill:none;stroke:#2b7fc0;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:640 640;
  animation:d-beat 2.4s linear infinite}
.deployment .d-beat .d-beat-base{stroke:#dceaf6;animation:none}

/* the move: two boxes and the visitors going from one to the other */
.deployment .d-move{display:grid;grid-template-columns:1fr auto 1fr;gap:14px;align-items:center;margin-top:16px}
.deployment .d-box{border:1.5px solid #d5dee3;border-radius:12px;padding:12px 14px;background:#fff;min-width:0}
.deployment .d-box b{display:block;font-size:12px;color:var(--quiet);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.deployment .d-box strong{font-size:22px;font-weight:700;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
.deployment .d-box small{display:block;font-size:12px;color:var(--quiet);margin-top:3px}
.deployment .d-box.new{border-color:#2b7fc0;box-shadow:0 0 0 4px #2b7fc018}
.deployment .d-box.new strong{color:#145e93}
.deployment .d-arrow{width:64px;height:24px;overflow:visible}
.deployment .d-arrow line{stroke:#c7d5dd;stroke-width:3;stroke-linecap:round}
.deployment .d-arrow circle{fill:#2b7fc0}
.deployment .d-arrow .a1{animation:d-travel 1.4s linear infinite}
.deployment .d-arrow .a2{animation:d-travel 1.4s linear infinite .45s}
.deployment .d-arrow .a3{animation:d-travel 1.4s linear infinite .9s}
.deployment .d-box.old.left strong{color:#647785}

/* the visitors: who is served by which version, as a picture, then as a bar */
.deployment .d-visitors .d-pad{padding:22px 24px}
.deployment .d-row{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap}
.deployment .d-flow{width:100%;height:auto;display:block;margin:14px 0 4px}
.deployment .d-flow text{font:600 12px system-ui;fill:var(--ink)}
.deployment .d-flow .d-flow-sub{font:500 11px system-ui;fill:var(--quiet)}
.deployment .d-flow .d-flow-pct{font:700 15px system-ui}
.deployment .d-flow .d-lane{fill:none;stroke:#e2eaee;stroke-width:3;stroke-linecap:round}
.deployment .d-flow .d-lane.on{stroke:#b9d3e8}
.deployment .d-flow .d-lane.on.old{stroke:#c9d3d9}
.deployment .d-flow .d-car{fill:#087f78}
.deployment .d-flow .d-car.old{fill:#8a9aa5}
.deployment .d-flow .d-vbox{fill:#fff;stroke:#d5dee3;stroke-width:1.5}
.deployment .d-flow .d-vbox.new{stroke:#087f78;stroke-width:2}
.deployment .d-flow .d-vbox.off{stroke-dasharray:4 4}
.deployment .d-flow .d-people{fill:#eef2f5;stroke:#d5dee3;stroke-width:1.5}
.deployment .d-split{display:flex;height:34px;border-radius:10px;overflow:hidden;background:#edf2f4;margin:12px 0 10px}
.deployment .d-split span{display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:600;color:white;white-space:nowrap;overflow:hidden;transition:width .9s ease}
.deployment .d-split .old,.deployment .d-legend .old{background:#8a9aa5}
.deployment .d-split .new,.deployment .d-legend .new{background:#087f78}
.deployment .d-legend{display:flex;gap:18px;flex-wrap:wrap;font-size:12px;color:var(--quiet)}
.deployment .d-legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.deployment .d-visitors p.d-say{color:var(--ink);font-size:14px;margin-top:12px;line-height:1.55}

/* the journey: every stage, in order, with what it does, why, and what happens if it fails */
.deployment .d-journey-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;border-bottom:0}
.deployment .d-count{font-size:13px;font-weight:600;white-space:nowrap;color:var(--quiet)}
.deployment .d-segments{display:flex;gap:4px;padding:0 20px 16px;border-bottom:1px solid #edf1f3}
.deployment .d-segments span{flex:1;height:8px;border-radius:4px;background:#e6ecef;transition:background .4s}
.deployment .d-segments .s-done{background:#2f9e78}
.deployment .d-segments .s-skipped{background:#bfe3d3}
.deployment .d-segments .s-running,.deployment .d-segments .s-next{background:repeating-linear-gradient(-45deg,#2b7fc0 0 6px,#6aaee0 6px 12px);
  background-size:34px 100%;animation:d-stripes 1s linear infinite}
.deployment .d-segments .s-failed,.deployment .d-segments .s-cancelled{background:#c8484e}
.deployment .d-segments .s-not_run{background:#efe9dc}
.deployment .d-stages{list-style:none;margin:0;padding:6px 0 10px}
.deployment .d-stage{display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:14px;padding:14px 20px;position:relative;align-items:start;
  border-left:3px solid transparent;transition:background .4s}
.deployment .d-stage:not(:last-child):before{content:'';position:absolute;left:41px;top:56px;bottom:-14px;width:2px;background:#e3eaee}
.deployment .d-stage.s-done:before,.deployment .d-stage.s-skipped:before{background:#a9d8c4}
.deployment .d-node{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:14px;background:#f4f6f8;color:#8796a1;
  border:2px solid #e3eaee;position:relative;z-index:1;transition:transform .3s,background .3s}
.deployment .d-stage.s-done .d-node,.deployment .d-stage.s-skipped .d-node{background:#2f9e78;color:white;border-color:#2f9e78}
.deployment .d-stage.s-running .d-node,.deployment .d-stage.s-next .d-node{background:#2b7fc0;color:white;border-color:#2b7fc0;animation:d-pulse 1.6s ease-out infinite}
.deployment .d-stage.s-running .d-node:after{content:'';position:absolute;inset:-8px;border-radius:50%;border:2.5px solid transparent;border-top-color:#2b7fc0;
  border-right-color:#2b7fc070;animation:d-spin 1s linear infinite}
.deployment .d-stage.s-failed .d-node,.deployment .d-stage.s-cancelled .d-node{background:#c8484e;color:white;border-color:#c8484e}
.deployment .d-stage.s-running,.deployment .d-stage.s-next{background:linear-gradient(90deg,#eaf3fc,#fff 80%);border-left-color:#2b7fc0}
.deployment .d-stage.s-failed,.deployment .d-stage.s-cancelled{background:linear-gradient(90deg,#fdeef0,#fff 80%);border-left-color:#c8484e}
.deployment .d-stage.d-just{animation:d-flash 1.4s ease-out}
.deployment .d-stage.d-just .d-node,.deployment .d-track .d-just .d-tn{animation:d-pop .6s cubic-bezier(.2,1.4,.4,1)}
.deployment .d-stage h3{font-size:15.5px;margin:3px 0 4px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.deployment .d-stage h3 .d-pill{font-size:10.5px;padding:2px 8px 2px 7px}
.deployment .d-stage.s-pending h3,.deployment .d-stage.s-not_run h3{color:#7c8b96}
.deployment .d-stage.s-not_run{opacity:.6}
.deployment .d-stage p{margin:0;font-size:13.5px;color:#41535f}
.deployment .d-stage .d-now{color:#145e93;font-weight:600;margin-top:6px}
.deployment .d-stage .d-why{margin-top:8px;padding-top:8px;gap:6px;border-top-color:#e3eaee}
.deployment .d-stage details.d-more>summary{font-size:12px;font-weight:500;color:var(--quiet)}
.deployment .d-stage .d-why div{font-size:12.5px;color:#55666f}
.deployment .d-stage .d-why i{width:18px;height:18px;font-size:10px;border-radius:5px;margin-top:1px}
.deployment .d-stage .d-why div{grid-template-columns:18px minmax(0,1fr)}
.deployment .d-when{text-align:right;min-width:96px}
.deployment .d-when strong{display:block;font-size:17px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.3px}
.deployment .d-when>span{font-size:11px;color:var(--quiet);text-transform:uppercase;letter-spacing:.06em;font-weight:700}
.deployment .d-stage.s-done .d-when>span{color:#176d58}
.deployment .d-stage.s-running .d-when>span,.deployment .d-stage.s-next .d-when>span{color:#2b7fc0}
.deployment .d-stage.s-failed .d-when>span{color:#a43439}
.deployment .d-stage .d-watch{margin-top:10px;max-width:460px}
.deployment .d-stage .d-watch-bar{height:9px}
.deployment .d-stage .d-watch-text{font-size:12px}
.deployment .d-stage details{margin-top:8px}
.deployment .d-stage details>summary{padding:0;font-size:12px;color:var(--quiet);display:inline-flex}
.deployment .d-stage details>summary:after{content:'+';margin-left:6px}
.deployment .d-stage details[open]>summary:after{content:'−'}
.deployment .d-substeps{list-style:none;margin:6px 0 0;padding:0;font-size:12px;max-width:520px}
.deployment .d-substeps li{display:flex;gap:8px;padding:3px 0;color:#41535f}
.deployment .d-substeps time{margin-left:auto;color:var(--quiet);font-variant-numeric:tabular-nums;padding-left:10px}

@keyframes d-stripes{to{background-position:34px 0}}
@keyframes d-flow{to{background-position:32px 0}}
@keyframes d-spin{to{transform:rotate(360deg)}}
@keyframes d-pulse{0%{box-shadow:0 0 0 0 #2b7fc070}70%{box-shadow:0 0 0 12px #2b7fc000}100%{box-shadow:0 0 0 0 #2b7fc000}}
@keyframes d-ripple{0%{transform:scale(.6);opacity:.9}100%{transform:scale(1.9);opacity:0}}
@keyframes d-pop{0%{transform:scale(.4)}60%{transform:scale(1.18)}100%{transform:scale(1)}}
@keyframes d-flash{0%{background:#dff3ea}100%{background:transparent}}
@keyframes d-shine{to{background-position:400px 0}}
@keyframes d-beat{0%{stroke-dashoffset:640}100%{stroke-dashoffset:-640}}
@keyframes d-travel{0%{transform:translateX(0);opacity:0}15%{opacity:1}85%{opacity:1}100%{transform:translateX(56px);opacity:0}}
@media(prefers-reduced-motion:reduce){.deployment *{animation:none!important;transition:none!important}
.deployment .d-flow .d-car,.deployment .d-arrow circle{display:none}}

html.d-present .side{display:none}
html.d-present .content{max-width:1320px;padding:24px 44px 48px}
html.d-present .deployment .d-columns{display:none}
html.d-present .deployment h1{font-size:40px}
html.d-present .deployment .d-board h2{font-size:32px}
html.d-present .deployment .d-board .d-lead{font-size:18px}
html.d-present .deployment .d-ring{width:132px;height:132px}
html.d-present .deployment .d-tl{font-size:13.5px}
html.d-present .deployment .d-now-card h2{font-size:27px}
html.d-present .deployment .d-now-card .d-say{font-size:17px}
html.d-present .deployment .d-why div{font-size:15px}
html.d-present .deployment .d-visitors p.d-say{font-size:16px}
html.d-present .deployment .d-stage{padding:16px 24px}
html.d-present .deployment .d-stage h3{font-size:19px}
html.d-present .deployment .d-stage p{font-size:15px}
html.d-present .deployment .d-stage .d-why div{font-size:14px}
html.d-present .deployment .d-when strong{font-size:21px}
html.d-present .deployment .d-split{height:46px}
html.d-present .deployment .d-split span{font-size:16px}

@media(max-width:1050px){.deployment .d-columns{grid-template-columns:1fr}
.deployment .d-columns>aside{display:contents}.deployment .d-columns>aside>.d-panel:first-child{grid-row:1}
.deployment .d-top{flex-direction:column}
.deployment .d-duo{grid-template-columns:1fr}
}

@media(max-width:760px){.deployment .d-tl{display:none}
.deployment .d-tn{width:22px;height:22px;font-size:10px}
.deployment .d-track li:before,.deployment .d-track li:after{top:15px}
.deployment .d-track .d-phase{font-size:9.5px;letter-spacing:.04em}
.deployment .d-board .d-stats{grid-template-columns:repeat(2,minmax(0,1fr))}
}

@media(max-width:520px){.deployment h1{font-size:27px}
.deployment .d-board{padding:18px}
.deployment .d-board-head{gap:12px}
.deployment .d-ring{width:76px;height:76px}
.deployment .d-board h2{font-size:20px}
.deployment .d-board .d-stats{grid-template-columns:1fr;gap:12px}
.deployment .d-stat{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.deployment .d-stat strong{font-size:16px;text-align:right}
.deployment .d-hero{padding:18px}
.deployment .d-hero-head{gap:12px}
.deployment .d-hero h2{font-size:19px}
.deployment .d-stats{grid-template-columns:1fr;gap:12px}
.deployment .d-list-stats{gap:8px}
.deployment .d-list-stats>div{padding:12px}
.deployment .d-step{grid-template-columns:25px minmax(0,1fr)}
.deployment .d-timing{grid-column:2}
.deployment .d-run{grid-template-columns:1fr}
.deployment .d-now-head{flex-direction:column}
.deployment .d-now-clock{text-align:left}
.deployment .d-move{grid-template-columns:1fr;gap:8px}
.deployment .d-arrow{transform:rotate(90deg);margin:6px auto;height:24px}
.deployment .d-stage{grid-template-columns:32px minmax(0,1fr);gap:10px;padding:12px 14px}
.deployment .d-stage:not(:last-child):before{left:29px}
.deployment .d-node{width:30px;height:30px;font-size:12px}
.deployment .d-when{grid-column:2;text-align:left;display:flex;gap:8px;align-items:baseline}
.deployment .d-when strong{font-size:14px}
.deployment .d-segments{padding:0 14px 14px}
}

@media print{.side,.d-tools,.d-back,#deployment-connection{display:none!important}
.shell,.main,.content{display:block!important;margin:0!important;padding:0!important}
.deployment .d-columns{display:block}
}

"""

SCRIPT = r"""
(() => {
  const root = document.getElementById('deployment-content');
  const connection = document.getElementById('deployment-connection');
  const announcement = document.getElementById('deployment-announcement');
  if (!root) return;
  // finished: the deployment ended, so nothing polls by itself; Refresh now still asks once.
  let busy = false, timer, errors = 0, stopped = false, finished = root.dataset.terminal === 'true';
  let previous = root.dataset.state;
  // The clocks on the page count from times the server read from GitHub and Google Cloud; the offset puts this browser's clock
  // on the server's, so a laptop whose clock is off still shows the right elapsed time.
  let offset = 0;
  const pad = n => String(n).padStart(2, '0');
  const span = s => (s = Math.max(0, Math.floor(s))) >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m` : `${Math.floor(s / 60)}m ${pad(s % 60)}s`;
  const clock = s => `${Math.floor((s = Math.max(0, Math.floor(s))) / 60)}:${pad(s % 60)}`;
  function synchronise() {
    const stamp = root.querySelector('[data-now]');
    offset = stamp ? Number(stamp.dataset.now) - Date.now() / 1000 : 0;
  }
  function tick() {
    if (document.hidden) return;
    const now = Date.now() / 1000 + offset;
    for (const el of root.querySelectorAll('[data-since]')) {
      const elapsed = now - Number(el.dataset.since), total = Number(el.dataset.window || 0);
      if (el.dataset.format === 'bar') el.style.width = Math.min(100, Math.max(0, elapsed / total * 100)) + '%';
      else if (el.dataset.format === 'watch') el.textContent = elapsed < total ? `${clock(elapsed)} of ${clock(total)} watched` : `${clock(total)} watched · finishing the check`;
      else el.textContent = span(elapsed);
    }
  }
  // In full screen the room cannot scroll: when a new stage starts, it is brought to the middle of the screen.
  let shown = root.querySelector('[aria-current="step"]')?.id;
  function follow() {
    const now = root.querySelector('[aria-current="step"]');
    if (now && now.id !== shown && document.documentElement.classList.contains('d-present')) {
      const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      now.scrollIntoView({behavior: still ? 'auto' : 'smooth', block: 'center'});
    }
    shown = now?.id;
  }
  function schedule() {
    clearTimeout(timer);
    if (!stopped && !finished && !document.hidden) timer = setTimeout(update, Math.min(60000, 10000 * 2 ** errors));
  }
  async function update() {
    if (busy || stopped || document.hidden) return;
    busy = true;
    connection.textContent = 'Checking for updates…';
    const timeout = new AbortController();
    const deadline = setTimeout(() => timeout.abort(), 20000);
    try {
      const response = await fetch(root.dataset.endpoint, {credentials:'same-origin',cache:'no-store',signal:timeout.signal,headers:{Accept:'application/json'}});
      if (response.status === 401 || response.status === 403 || response.status === 404) {
        stopped = true;
        connection.textContent = response.status === 401 ? 'Sign-in ended. Reload this page to sign in.' : 'This deployment is no longer available to your account.';
        announcement.textContent = connection.textContent;
        return;
      }
      if (!response.ok) throw new Error('Unavailable');
      const data = await response.json();
      const expanded = new Map(Array.from(root.querySelectorAll('details[id]'), el => [el.id, el.open]));
      const active = root.contains(document.activeElement) ? document.activeElement : null;
      const focused = active ? {id:active.id,href:active.getAttribute('href')} : null;
      // The visitors bar and the ring slide from where they were to where the evidence puts them now, rather than jumping.
      const widths = new Map(Array.from(root.querySelectorAll('[data-bar]'), el => [el.dataset.bar, el.style.width]));
      const rings = new Map(Array.from(root.querySelectorAll('[data-ring]'), el => [el.dataset.ring, el.style.strokeDasharray]));
      // A stage that just finished, or just started, gets its one-time effect; one that stays as it was gets none.
      const states = new Map(Array.from(root.querySelectorAll('[data-stage]'), el => [el.dataset.stage, el.dataset.stageState]));
      root.innerHTML = data.html;
      for (const el of root.querySelectorAll('[data-bar]')) {
        const before = widths.get(el.dataset.bar), after = el.style.width;
        if (before && before !== after) { el.style.width = before; void el.offsetWidth; el.style.width = after; }
      }
      for (const el of root.querySelectorAll('[data-ring]')) {
        const before = rings.get(el.dataset.ring), after = el.style.strokeDasharray;
        if (before && before !== after) { el.style.strokeDasharray = before; void el.offsetWidth; el.style.strokeDasharray = after; }
      }
      for (const el of root.querySelectorAll('[data-stage]')) {
        const before = states.get(el.dataset.stage), after = el.dataset.stageState;
        if (before !== undefined && before !== after && after !== 'pending') el.classList.add('d-just');
      }
      synchronise(); tick(); follow();
      for (const [id, open] of expanded) {
        const element = document.getElementById(id);
        if (element) element.open = open;
      }
      if (focused) {
        const target = focused.id ? document.getElementById(focused.id) :
          Array.from(root.querySelectorAll('a[href]')).find(el => el.getAttribute('href') === focused.href);
        target?.focus({preventScroll:true});
      }
      if (data.state !== previous) announcement.textContent = data.label;
      previous = data.state;
      errors = 0;
      finished = data.terminal === true;
      connection.textContent = data.error ? 'Provider connection interrupted · showing last known progress' :
        (finished ? 'Finished · automatic updates off · checked ' : 'Automatic updates on · page checked ') + new Date().toLocaleTimeString();
    } catch (_) {
      errors = Math.min(errors + 1, 3);
      connection.textContent = 'Connection interrupted · progress may be out of date · retrying automatically';
      announcement.textContent = 'Unable to refresh deployment status. Last known progress remains visible.';
    } finally {
      clearTimeout(deadline); busy = false; schedule();
    }
  }
  document.getElementById('deployment-refresh')?.addEventListener('click', update);
  // Full screen: the sidebar and the technical panels step aside and the stages grow, for a room watching on a projector.
  const present = document.getElementById('deployment-present'), page = document.documentElement;
  present?.addEventListener('click', () => {
    const on = !page.classList.contains('d-present');
    page.classList.toggle('d-present', on);
    present.textContent = on ? 'Exit full screen' : 'Full screen';
    if (on && page.requestFullscreen) page.requestFullscreen().catch(() => {});
    if (on) { shown = null; follow(); }
    if (!on && document.fullscreenElement) document.exitFullscreen().catch(() => {});
  });
  document.addEventListener('fullscreenchange', () => {
    if (!document.fullscreenElement && page.classList.contains('d-present')) {
      page.classList.remove('d-present');
      if (present) present.textContent = 'Full screen';
    }
  });
  synchronise(); tick();
  setInterval(tick, 1000);
  document.addEventListener('visibilitychange', () => {clearTimeout(timer); if (!document.hidden && !finished) update();});
  window.addEventListener('pagehide', () => {stopped = true; clearTimeout(timer);});
  window.addEventListener('pageshow', event => {if(event.persisted){stopped=false; if(!finished) update();}});
  schedule();
})();
"""


def moment(value, short=False):
    if not value or value <= 0:
        return "Not reported"
    return datetime.datetime.fromtimestamp(value, datetime.UTC).strftime("%H:%M:%S" if short else "%b %d, %H:%M:%S UTC")


def duration(value):
    if value is None or value < 0:
        return "Not reported"
    seconds = int(value)
    return f"{seconds // 3600}h {seconds % 3600 // 60}m" if seconds >= 3600 else f"{seconds // 60}m {seconds % 60:02d}s"


def step_name(name):
    return {"Validate artifact and deployment configuration": "Check the deployment destination",
            "Push image and resolve immutable digest": "Upload the app to Google Cloud",
            "Deploy, probe, promote; restore traffic on failure": "Start the app, check health, and switch traffic",
            "Audit Python dependencies": "Check Python packages for security issues",
            "Audit Node.js dependencies": "Check Node.js packages for security issues",
            "Build without cloud credentials": "Build the app image",
            "Run unit tests": "Run automated tests", "Run unit tests (Node.js)": "Run automated tests"}.get(name, name)


def link(href, label, primary=False):
    return f'<a class="btn {"primary" if primary else "ghost"}" href="{ui.esc(href)}">{ui.esc(label)}</a>'


def tone(state):
    if state in {"succeeded", "done", "passed", "passed_in_earlier_release", "rolled_back"}:
        return "done"
    if state in {"failed", "blocked", "timed_out", "dispatch_lost"}:
        return "failed"
    return "running" if state in {"running", "dispatched", "dispatching", "in_progress"} else "skipped"


def pill(state, label):
    """A state as a pill; its dot ripples while the deployment goes on."""
    return f'<span class="d-pill {tone(state)}"><i aria-hidden="true"></i>{ui.esc(label)}</span>'


def badge(state, label=None):
    plain = {"passed_in_earlier_release": "Passed previously", "skipped_by_config": "Not required", "unconfirmed": "Not verified",
             "release_failed_health_unconfirmed": "Not verified", "not_run": "Did not run", "reported": "Results reported", "pending": "Waiting"}
    return f'<span class="d-badge {tone(state)}">{ui.esc(label or plain.get(state) or LABELS.get(state, state.replace("_", " ").capitalize()))}</span>'


def panel(title, body, sub=""):
    return f'<section class="d-panel"><div class="d-panel-head"><h2>{ui.esc(title)}</h2>{f"<p>{ui.esc(sub)}</p>" if sub else ""}</div>{body}</section>'


def facts(items):
    return '<dl class="d-facts">' + "".join(f"<div><dt>{ui.esc(label)}</dt><dd>{ui.esc(value if value is not None else 'Not reported')}</dd></div>" for label, value in items) + "</dl>"


def ring(done, total):
    fill = round(251.3 * done / total, 2) if total else 0
    return (
        f'<svg class="d-ring" viewBox="0 0 100 100" role="img" aria-label="{done} of {total} reported steps finished">'
        '<circle cx="50" cy="50" r="40" fill="none" stroke="#e1ecec" stroke-width="7"/>'
        f'<circle cx="50" cy="50" r="40" fill="none" stroke="#087f78" stroke-width="7" stroke-dasharray="{fill} 251.3" '
        'transform="rotate(-90 50 50)" stroke-linecap="round"/>'
        f'<text x="50" y="57" text-anchor="middle">{f"{done}/{total}" if total else "—"}</text></svg>'
    )


STAGE_WORDS = {"done": "Done", "running": "Now", "next": "Starting", "pending": "Waiting", "failed": "Stopped here", "cancelled": "Cancelled",
               "skipped": "Not needed", "not_run": "Did not run"}
STAGE_MARKS = {"done": "✓", "skipped": "✓", "failed": "!", "cancelled": "×", "not_run": "–"}
STEP_MARKS = {"done": "✓", "running": "•", "failed": "!", "skipped": "−", "cancelled": "×"}

# Each stage in full, for a person who has never seen a release: the short name on the track, what happens during it, why it is
# there, and what happens if it fails. The words are plain on purpose; the stage's one-line text comes from journey.STAGES.
GUIDE = {
    "request": ("Request",
                "The platform checks that this is exactly the change that was approved: the same commit, the same settings file, the same destination.",
                "Nothing can slip in between the approval and the release.",
                "If anything does not match, the release stops before any code runs."),
    "code": ("Code safety",
             "The Dockerfile is checked against the lab's rules, and every file is searched for a password or key committed by mistake.",
             "A leaked secret or an unsafe container setting is caught before anything is built.",
             "If a secret or a broken rule is found, the release stops here. Nothing is uploaded."),
    "tests": ("Tests",
              "The app's own automated tests run on a clean machine, the way its developer wrote them.",
              "Passing tests show the change behaves the way its developer expected.",
              "One failing test stops the release. Visitors never see the change."),
    "libraries": ("Libraries",
                  "Every library the app uses is looked up in public lists of known security problems.",
                  "A library with a known hole is the most common way an app gets attacked.",
                  "A serious problem that already has a fix stops the release until the library is updated."),
    "build": ("Build",
              "The app is packed into a container image on a machine that holds no cloud password. The image is searched for secrets, and its parts are listed.",
              "The image is built once, checked once, and then runs unchanged everywhere.",
              "A build error, or a secret inside the image, stops the release."),
    "sign": ("Upload",
             "The image is uploaded to Google Cloud and signed with a key only the platform holds.",
             "Google Cloud runs only an image that carries this signature, so a changed image cannot start.",
             "If the upload or the signature fails, nothing changes for visitors."),
    "scan": ("Scan",
             "Google scans the uploaded image for known security problems. The result is saved with this release.",
             "You can always see what the image contained on the day it went live.",
             "A serious problem that already has a fix stops the release; the summary says which."),
    "start": ("Start",
              "A copy of the new version starts next to the version people use now. It gets no visitors yet. "
              "The platform calls its health address and waits for a healthy answer.",
              "The new version proves it can start and answer before anyone is sent to it.",
              "If it does not become healthy, it is removed. Visitors stay on the old version and notice nothing."),
    "switch": ("Switch",
               "Visitors are sent to the new version, and its health is checked once more. The old version keeps running, ready to take visitors back.",
               "Because the old version is still running, going back takes seconds, not minutes.",
               "If the new version fails during the switch, visitors go back to the old version by themselves."),
    "watch": ("Watch",
              "For {minutes} the platform watches the new version's errors, crashes and slow answers while real visitors use it.",
              "Some problems only show up with real traffic.",
              "If errors rise, visitors go back to the old version on their own. Nobody has to act."),
    "record": ("Record",
               "The address, the checks and the outcome are saved with this release and posted where the team can see them.",
               "Every release leaves a receipt you can read later.",
               "If the recording fails, the app is still live; only the receipt is missing."),
}
# The track groups the stages by what the group does for the person watching.
PHASES = (("Check the change", ("request", "code", "tests", "libraries")), ("Build and sign", ("build", "sign", "scan")),
          ("Go live", ("start", "switch", "watch")), ("Record", ("record",)))


def clock(seconds):
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


def minutes_words(seconds):
    minutes = round((seconds or 0) / 60) or 1
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def ticking(since, now, words=None):
    """Elapsed time that the page's script keeps counting each second from a time the server read; words while it is unknown."""
    if not since:
        return ui.esc(words or "—")
    return f'<span data-since="{since:.0f}">{duration(now - since)}</span>'


def stage_time(stage, now):
    if stage["state"] in {"running", "next"}:
        return ticking(stage.get("started_at"), now, "…")
    if stage.get("started_at") and stage.get("finished_at"):
        return duration(stage["finished_at"] - stage["started_at"]) if stage["finished_at"] - stage["started_at"] >= 1 else ""
    return duration(stage["seconds"]) if stage.get("seconds") is not None else ""


def guide(stage, bake=0):
    """(short name, what happens, why it is there, what happens if it fails) for a stage, with the watch window filled in."""
    short, what, why, fail = GUIDE.get(stage["key"]) or (stage["title"], stage["text"], "", "")
    return short, what.replace("{minutes}", minutes_words(bake)), why, fail


def why_block(why, fail, done=False, compact=False, ident=""):
    """Why a stage is there and what happens if it fails, as two short labelled lines, closed until someone opens them. The id
    lets the page keep it open across its automatic refresh."""
    if not why and not fail:
        return ""
    rows = ""
    if why:
        rows += f'<div class="w-why"><i aria-hidden="true">?</i><span><b>Why it is here</b>{ui.esc(why)}</span></div>'
    if fail:
        rows += (f'<div class="{"w-done" if done else "w-fail"}"><i aria-hidden="true">!</i>'
                 f'<span><b>If it fails</b>{ui.esc(fail)}</span></div>')
    return (f'<details class="d-more" id="{ui.esc(ident)}"><summary id="{ui.esc(ident)}-summary">Why it is here · If it fails</summary>'
            f'<div class="d-why">{rows}</div></details>')


def watch_visual(stage, now, big=False):
    """The watch as a bar filling over its window; on the spotlight, under a line that keeps beating."""
    since, window = stage.get("since"), stage.get("window")
    if not (since and window):
        return ""
    elapsed = max(0, now - since)
    width = min(100, elapsed / window * 100)
    words = f"{clock(elapsed)} of {clock(window)} watched" if elapsed < window else f"{clock(window)} watched · finishing the check"
    beat = ""
    if big:
        # One heartbeat every 120 units, five across the line.
        line = "M0 23 " + " ".join(f"H{x + 60} L{x + 68} 23 L{x + 74} 8 L{x + 80} 38 L{x + 86} 16 L{x + 92} 23" for x in range(0, 600, 120)) + " H640"
        beat = (f'<svg class="d-beat" viewBox="0 0 640 46" preserveAspectRatio="none" aria-hidden="true">'
                f'<path class="d-beat-base" d="{line}"/><path d="{line}"/></svg>')
    return (f'<div class="d-watch">{beat}<div class="d-watch-bar" role="img" aria-label="Watching the new version for {clock(window)} minutes">'
            f'<span data-since="{since:.0f}" data-window="{window}" data-format="bar" style="width:{width:.1f}%"></span></div>'
            f'<div class="d-watch-text" data-since="{since:.0f}" data-window="{window}" data-format="watch">{words}</div></div>')


def move_visual(stage, data):
    """The start and the switch as two boxes: the version people use now, the new one, and the visitors going across."""
    share = float(stage.get("share") or 0) if stage["state"] == "running" else 0.0
    if stage["key"] == "start":
        present = (data.get("cloud") or {}).get("present")
        new_words, new_sub = ("Running", "No visitors yet · health being checked") if present else ("Starting", "Getting ready")
        old_words, old_sub = "100%", "of visitors"
    else:
        new_words, new_sub = f"{share:g}%", "of visitors"
        old_words, old_sub = f"{100 - share:g}%", "of visitors"
    arrow = ('<svg class="d-arrow" viewBox="0 0 64 24" aria-hidden="true"><line x1="4" y1="12" x2="60" y2="12"/>'
             + "".join(f'<circle class="a{n}" cx="4" cy="12" r="3.5"/>' for n in (1, 2, 3)) + '</svg>')
    return (f'<div class="d-move"><div class="d-box old left"><b>Previous version</b><strong>{old_words}</strong><small>{old_sub}</small></div>{arrow}'
            f'<div class="d-box new"><b>This release</b><strong>{new_words}</strong><small>{new_sub}</small></div></div>')


def now_panel(data):
    """The spotlight: the one stage that matters right now, said in full, with what it is doing, why, and what happens if it fails."""
    stages, now, bake, state = data["stages"], data["now"], data.get("bake_seconds") or 0, data["state"]
    current = next((s for s in stages if s["state"] in {"running", "next"}), None)
    stopped = next((s for s in stages if s["state"] in {"failed", "cancelled"}), None)
    body, cls, clock_html = "", "", ""
    if stopped:
        short, what, why, fail = guide(stopped, bake)
        cls = "n-stop"
        eyebrow, title = "Stopped here", stopped["title"]
        body = f'<p class="d-say">{ui.esc(fail)}</p><div class="d-why">'
        body += f'<div class="w-why"><i aria-hidden="true">?</i><span><b>What this stage does</b>{ui.esc(what)}</span></div>'
        if data.get("next_action"):
            body += f'<div class="w-fail"><i aria-hidden="true">→</i><span><b>What to do now</b>{ui.esc(data["next_action"])}</span></div>'
        body += "</div>"
        took = stage_time(stopped, now)
        clock_html = f'<div class="d-now-clock"><strong>{took or "—"}</strong><span>{"Ran for" if took else "Stopped"}</span></div>'
    elif current and not data.get("terminal"):
        short, what, why, fail = guide(current, bake)
        eyebrow = "Happening now" if current["state"] == "running" else "Starting next"
        title = current["title"]
        note = current.get("note") if current["state"] == "running" else "GitHub is handing over to this stage."
        body = f'<p class="d-say">{ui.esc(what)}</p>'
        if note:
            body += f'<p class="d-say d-now">{ui.esc(note)}</p>'
        if current["key"] in {"start", "switch"} and current["state"] == "running":
            body += move_visual(current, data)
        if current["key"] == "watch" and current["state"] == "running":
            body += watch_visual(current, now, big=True)
        body += why_block(why, fail, ident=f"now-why-{current['key']}")
        clock_html = f'<div class="d-now-clock"><strong>{ticking(current.get("started_at"), now, "…")}</strong><span>So far</span></div>'
    elif state in {"succeeded", "rolled_back"} and data.get("terminal"):
        cls, eyebrow, title = "n-live", "Finished", "The new version is live" if state == "succeeded" else "Visitors were sent back"
        body = ('<p class="d-say">Every stage passed. The new version served everyone, was watched with real traffic, and the result is recorded.</p>'
                if state == "succeeded" else
                '<p class="d-say">Every stage passed, and later the visitors were sent back to the version before this one.</p>')
        app_url = (data.get("health") or {}).get("url") or data.get("app_url")
        if app_url and state == "succeeded":
            body += link(app_url, "Open the app ↗", True)
        elapsed = (data.get("finished_at") or now) - data["started_at"] if data.get("started_at") else None
        clock_html = f'<div class="d-now-clock"><strong>{ui.esc(duration(elapsed)) if elapsed is not None else "—"}</strong><span>Took</span></div>'
    elif state == "succeeded":
        cls, eyebrow, title = "n-wait", "Almost done", "GitHub has finished"
        body = '<p class="d-say">The platform is recording the result and the checks. This usually takes under a minute.</p>'
    else:
        cls, eyebrow, title = "n-wait", "Waiting", data["label"]
        body = f'<p class="d-say">{ui.esc(step_name(data.get("current")) or "Waiting for GitHub to report the first stage.")}</p>'
    return (f'<section class="d-panel d-now-card {cls}" aria-live="off"><div class="d-pad"><div class="d-now-head"><div><div class="d-eyebrow">{eyebrow}</div>'
            f'<h2>{ui.esc(title)}</h2></div>{clock_html}</div>{body}</div></section>')


def track(stages):
    """Every stage as a node on one line, grouped by what the group does, each a link to its row in the journey."""
    keys = [s["key"] for s in stages]
    phases = []
    for name, members in PHASES:
        count = sum(k in members for k in keys)
        if not count:
            continue
        states = {s["state"] for s in stages if s["key"] in members}
        cls = ("p-stop" if states & {"failed", "cancelled"} else "p-now" if states & {"running", "next"} else
               "p-done" if states <= {"done", "skipped"} else "")
        phases.append(f'<span class="d-phase {cls}" style="grid-column:span {count}">{ui.esc(name)}</span>')
    nodes = []
    for index, stage in enumerate(stages):
        state = stage["state"]
        before = stages[index - 1]["state"] if index else None
        after = stages[index + 1]["state"] if index + 1 < len(stages) else None
        incoming = ("in-flow" if state in {"running", "next"} else "in-done" if before in {"done", "skipped"} and state not in {"pending", "not_run"} else
                    "in-stop" if before in {"failed", "cancelled"} else "")
        outgoing = ("out-done" if state in {"done", "skipped"} and after not in {None, "pending", "not_run"} else
                    "out-stop" if state in {"failed", "cancelled"} else "")
        short = guide(stage)[0]
        mark = STAGE_MARKS.get(state, index + 1)
        nodes.append(f'<li class="t-{ui.esc(state)} {incoming} {outgoing}" data-stage="track-{ui.esc(stage["key"])}" data-stage-state="{ui.esc(state)}">'
                     f'<a href="#stage-{ui.esc(stage["key"])}" title="{ui.esc(stage["title"])} · {STAGE_WORDS.get(state, state)}">'
                     f'<span class="d-tn" aria-hidden="true">{mark}</span><span class="d-tl">{ui.esc(short)}</span>'
                     f'<span class="d-announcement">{ui.esc(stage["title"])}: {STAGE_WORDS.get(state, state)}</span></a></li>')
    return (f'<nav class="d-track" aria-label="Stages of this release"><div class="d-phases">{"".join(phases)}</div>'
            f'<ol>{"".join(nodes)}</ol></nav>')


def journey_panel(data):
    """The release as stages a person can follow: each one lights up when GitHub or Google Cloud reports it."""
    stages, now, bake = data["stages"], data["now"], data.get("bake_seconds") or 0
    done = sum(stage["state"] in {"done", "skipped"} for stage in stages)
    rows = []
    for index, stage in enumerate(stages):
        state, key = stage["state"], stage["key"]
        short, what, why, fail = guide(stage, bake)
        title = stage["title"] + (f" · {round(bake / 60) or 1} min" if key == "watch" and bake else "")
        extra = ""
        if stage.get("note") and state == "running":
            extra += f'<p class="d-now">{ui.esc(stage["note"])}</p>'
        if stage.get("share") and state == "running":
            extra += f'<p class="d-now">{stage["share"]:g}% of visitors are on the new version.</p>'
        if key == "watch" and state == "running":
            extra += watch_visual(stage, now)
        more = why_block(why, fail, done=state in {"done", "skipped"}, compact=True, ident=f"stage-why-{key}")
        steps = stage.get("steps") or []
        if steps:
            items = "".join(
                f'<li><span class="d-symbol {ui.esc(step["state"])}" aria-hidden="true">'
                f'{STEP_MARKS.get(step["state"], "○")}</span>'
                f'<span title="{ui.esc(step["name"])}">{ui.esc(step_name(step["name"]))}</span>'
                f'<time>{duration(step["finished_at"] - step["started_at"]) if step.get("started_at") and step.get("finished_at") else ""}</time></li>'
                for step in steps)
            more += (f'<details id="stage-steps-{key}"><summary id="stage-steps-summary-{key}">{len(steps)} GitHub step{"s" if len(steps) != 1 else ""}</summary>'
                     f'<ul class="d-substeps">{items}</ul></details>')
        if more:
            # The two closed sections side by side; an open one takes the whole width.
            extra += f'<div class="d-links">{more}</div>'
        current = ' aria-current="step"' if state in {"running", "next"} else ""
        marked = pill("running" if state == "next" else state, STAGE_WORDS.get(state, state)) if state in {"running", "next", "failed", "cancelled"} else ""
        rows.append(
            f'<li class="d-stage s-{ui.esc(state)}" id="stage-{key}" data-stage="{key}" data-stage-state="{ui.esc(state)}"{current}>'
            f'<span class="d-node" aria-hidden="true">{STAGE_MARKS.get(state, index + 1)}</span>'
            f'<div><h3>{ui.esc(title)}{marked}</h3><p>{ui.esc(what)}</p>{extra}</div>'
            f'<div class="d-when"><strong>{stage_time(stage, now)}</strong><span>{STAGE_WORDS.get(state, state)}</span></div></li>')
    segments = "".join(f'<span class="s-{ui.esc(stage["state"])}" title="{ui.esc(stage["title"])}"></span>' for stage in stages)
    return (f'<section class="d-panel d-journey" data-now="{now:.0f}"><div class="d-panel-head d-journey-head"><div><h2>Release journey</h2>'
            '<p>Every stage turns green when GitHub or Google Cloud reports it. The times are real, not estimates. '
            'Open “Why it is here” under a stage to see why it is there and what happens if it fails.</p></div>'
            f'<span class="d-count">{done} of {len(stages)} done</span></div><div class="d-segments" aria-hidden="true">{segments}</div>'
            f'<ol class="d-stages">{"".join(rows)}</ol></section>')


def flow_visual(old, new, present):
    """Visitors on the left; the two versions on the right; a lane to each, with traffic moving along the lanes that carry any."""
    def lane(y, share, cls):
        on = " on" if share > 0 else ""
        path = f"M118 70 C 170 70, 170 {y}, 222 {y}"
        cars = ""
        if share > 0:
            count = 1 if share < 25 else 2 if share < 60 else 3
            for n in range(count):
                # Each car starts part of the way along, so none waits at the picture's corner for its turn.
                offset = n / count
                keys = (f'keyPoints="{offset:.2f};1;0;{offset:.2f}" keyTimes="0;{1 - offset:.2f};{1 - offset:.2f};1" calcMode="linear"' if n else "")
                cars += f'<circle class="d-car {cls}" r="4"><animateMotion dur="2.2s" repeatCount="indefinite" {keys} path="{path}"/></circle>'
        return f'<path class="d-lane{on} {cls}" d="{path}"/>' + cars
    def box(y, cls, name, share, extra=""):
        off = " off" if (cls == "new" and not present and share <= 0) else ""
        return (f'<rect class="d-vbox {cls}{off}" x="222" y="{y - 22}" width="186" height="44" rx="10"/>'
                f'<text x="236" y="{y - 3}">{name}</text><text class="d-flow-sub" x="236" y="{y + 13}">{extra}</text>'
                f'<text class="d-flow-pct" x="396" y="{y + 2}" text-anchor="end" fill="{"#087f78" if cls == "new" else "#647785"}">{share:g}%</text>')
    new_extra = "everyone is here" if new >= 100 else "getting visitors" if new > 0 else "running, no visitors yet" if present else "not started yet"
    old_extra = "everyone is here" if old >= 100 else "still serving some" if old > 0 else "standing by, ready"
    return (f'<svg class="d-flow" viewBox="0 0 418 140" role="img" aria-label="Previous version {old:g} percent, this release {new:g} percent">'
            '<rect class="d-people" x="10" y="48" width="108" height="44" rx="10"/>'
            '<text x="24" y="66">Visitors</text><text class="d-flow-sub" x="24" y="82">people using the app</text>'
            f'{lane(32, old, "old")}{lane(108, new, "new")}'
            f'{box(32, "old", "Previous version", old, old_extra)}{box(108, "new", "This release", new, new_extra)}</svg>')


def visitors_panel(data):
    """Who gets the visitors now, as Cloud Run says: the previous version or this release, and what that means."""
    health, cloud = data.get("health") or {}, data.get("cloud") or {}
    traffic = health.get("traffic") or []
    if not traffic:
        return ""
    new = sum(float(t.get("percent") or 0) for t in traffic if t.get("plan_id") == data.get("id"))
    old = max(0.0, min(100.0, sum(float(t.get("percent") or 0) for t in traffic) - new))
    new = max(0.0, min(100.0, new))
    minutes = round((data.get("bake_seconds") or 0) / 60)
    if data["state"] in {"failed", "cancelled", "timed_out", "dispatch_lost"}:
        say = ("Visitors went back to the previous version." if cloud.get("traffic_at") else
               "Visitors stayed on the previous version; the new one never served anyone.")
    elif data.get("terminal") or data["state"] in {"succeeded", "rolled_back"}:
        say = ("Everyone is on this release." if new >= 100 else f"{new:g}% of visitors are on this release." if new else
               "This release no longer serves visitors: a newer release or a rollback has replaced it.")
    elif new >= 100:
        say = ("Everyone is on the new version. The old one stays ready" +
               (f": if the new one fails in the next {minutes} minute{'s' if minutes != 1 else ''}, visitors go back to it on their own." if minutes else "."))
    elif new > 0:
        say = f"Visitors are moving: {new:g}% are on the new version."
    elif cloud.get("present"):
        say = "The new version is running beside the old one with no visitors yet, while its health is checked."
    else:
        say = "Everyone is on the previous version. The new one has not started yet."

    def label(name, share):
        return f"{name} · {share:g}%" if share >= 18 else ""
    bar = (f'<div class="d-split" role="img" aria-label="Previous version {old:g} percent, this release {new:g} percent">'
           f'<span class="old" data-bar="old" style="width:{old:g}%">{label("Previous version", old)}</span>'
           f'<span class="new" data-bar="new" style="width:{new:g}%">{label("This release", new)}</span></div>')
    legend = (f'<div class="d-legend"><span><i class="old"></i>Previous version · {old:g}%</span><span><i class="new"></i>This release · {new:g}%</span></div>')
    return (f'<section class="d-panel d-visitors"><div class="d-pad"><div class="d-row"><h2>Who gets the visitors</h2>'
            f'<span class="d-caption">Read from Google Cloud at {moment(health.get("at"), True)} UTC</span></div>'
            f'{flow_visual(old, new, bool(cloud.get("present")))}{bar}{legend}'
            f'<p class="d-say">{ui.esc(say)}</p></div></section>')


def workflow_groups(data):
    groups = []
    for index, job in enumerate(data["groups"]):
        steps = []
        for step in job["steps"]:
            state = step["state"]
            mark = {"done": "✓", "running": "•", "failed": "!", "skipped": "−", "cancelled": "×"}.get(state, "○")
            elapsed = step.get("seconds")
            if step.get("started_at"):
                elapsed = (step.get("finished_at") or data["now"]) - step["started_at"]
            state_label = {"done": "Passed", "pending": "Not started", "running": "In progress", "unknown": "Not confirmed"}.get(state, state.capitalize())
            steps.append(
                f'<li class="d-step"><span class="d-symbol {state}" aria-hidden="true">{mark}</span>'
                f'<div class="d-step-name" title="{ui.esc(step["name"])}">{ui.esc(step_name(step["name"]))}<div class="d-caption">{state_label}</div></div>'
                f'<span class="d-timing">{duration(elapsed) if elapsed is not None else ""}</span></li>'
            )
        opened = any(s["state"] in ({"failed"} if data.get("stages") else {"running", "failed"}) for s in job["steps"]) or (
            len(data["groups"]) == 1 and not data.get("terminal") and not data.get("stages"))
        job_link = '<div class="d-pad">' + link(job["url"], "Open this job’s logs ↗") + "</div>" if job.get("url") else ""
        job_name = {"build": "Build and test", "deploy": "Deploy and verify", "promote": "Release to production", "notify": "Record and share the result"}.get(job["name"], job["name"])
        groups.append(
            f'<details class="d-job" id="job-{index}"{" open" if opened else ""}><summary id="job-summary-{index}">'
            f'{badge(job["state"])}<span class="d-job-name">{ui.esc(job_name)}</span></summary><ol class="d-steps">{"".join(steps)}</ol>{job_link}</details>'
        )
    title, sub = (("Every GitHub step", "The exact steps GitHub reports, by job, for the technical reader.") if data.get("stages") else
                  ("Deployment steps", "Actual recorded steps. Some jobs may run at the same time."))
    return panel(title, "".join(groups) or '<div class="d-empty">Waiting for GitHub to report the workflow steps.</div>', sub)


def detail(data):
    state, app = data["state"], data["application"]
    setup = data["kind"] == "setup"
    steps = [s for job in data["groups"] for s in job["steps"]]
    done = sum(s["state"] in {"done", "skipped"} for s in steps)
    total = len(steps)
    elapsed = (data.get("finished_at") or data["now"]) - data["started_at"] if data.get("started_at") else None
    label = data["label"]
    if state == "succeeded" and data.get("kind") in {"rollback", "production_rollback"}:
        label = "Recovery workflow finished"
    elif state == "succeeded" and not data.get("phases"):
        label = "Workflow passed · confirming release"
    explanation = step_name(data.get("current")) or {
        "planned": "The deployment has been prepared. It has not started yet.",
        "succeeded": "Review the checks and current service status below.",
        "completed_unverified": "The workflow ended, but some required checks have not been confirmed.",
        "failed": "Open the failed step to see what happened and what to do next.",
        "awaiting_approval": "A reviewer must approve the request in GitHub before it can continue.",
        "complete": "Setup has finished. Waiting for the first deployment record.",
    }.get(state, "This page updates automatically. You can leave and come back to this deployment.")
    tools = link("/settings?tab=deployments", "All deployments")
    if data.get("run_url"):
        tools += link(data["run_url"], "GitHub logs ↗")
    if data.get("id"):
        tools += link("/settings/deployment/report?" + urlencode({"plan": data["id"]}), "Download report")
    head = (f'<div class="d-top"><div><div class="d-eyebrow">Deployment workspace</div><h1>{ui.esc(app)}</h1>'
            f'<p>{ui.esc(data.get("environment"))} · {ui.esc(data.get("repository"))}</p></div><div class="d-tools">{tools}</div></div>')
    remaining = "Finished" if data.get("terminal") else "Waiting for approval" if state == "awaiting_approval" else "Not estimated"
    if state in {"blocked", "failed", "timed_out", "cancelled", "dispatch_lost"}:
        remaining = "Stopped"
    stages = data.get("stages")
    if stages:
        hero = stage_hero(data, state, label, elapsed)
    else:
        hero = (
            f'<section class="d-hero"><div class="d-hero-head">{ring(done, total)}<div>{badge(state)}<h2>{ui.esc(label)}</h2><p>{ui.esc(explanation)}</p></div></div>'
            '<div class="d-stats">'
            + "".join(
                f'<div class="d-stat"><span class="d-caption">{k}</span><strong>{ui.esc(v)}</strong></div>'
                for k, v in (("Elapsed time", duration(elapsed)), ("Steps remaining", str(total - done) if total else "Waiting for workflow"), ("Estimated time left", remaining))
            )
            + '</div><p class="d-caption">Progress counts reported steps, including skipped work. Time remaining is not estimated yet.</p></section>'
        )
    notices = ""
    if data.get("error"):
        notices += f'<div class="d-notice d-error" role="status">{ui.esc(data["error"])}</div>'
    if state in {"blocked", "failed", "timed_out", "cancelled", "awaiting_approval", "completed_unverified", "dispatch_unknown", "dispatch_lost"}:
        notices += f'<div class="d-notice"><strong>What needs your attention</strong><p>{ui.esc(data.get("next_action"))}</p>'
        for pending in data.get("pending") or []:
            notices += f"<p>{ui.esc(pending.get('environment'))} · Reviewers: {ui.esc(', '.join(pending.get('reviewers') or []) or 'See GitHub')}</p>"
        notices += "</div>"
    for item in data.get("links") or []:
        notices += link(item["url"], item["name"] + " ↗")
    check_names = {"dependency_scan": "Dependency safety", "health": "Deployment health check", "target": "Destination check"}
    checks = "".join(
        f'<div class="d-check"><span>{ui.esc(check_names.get(k, k.replace("_", " ").capitalize()))}</span>'
        f"{badge(v)}</div>"
        for k, v in data.get("checks", {}).items()
    )
    checks = panel("Checks & safeguards", '<div class="d-pad d-checks">' + (checks or "<p>Checks will appear when the workflow reports them.</p>") + "</div>")
    health = data.get("health") or {}
    if health:
        health_text = "Cloud Run service ready" if health.get("ready") else "Cloud Run service not ready"
        health_body = badge("done" if health.get("ready") else "failed", health_text)
        health_body += f'<p class="d-caption">Checked {moment(health.get("at"))}. Service readiness is separate from the health checks for this deployment.</p>'
        revision = (data.get("phases") or {}).get("revision")
        traffic_rows = health.get("traffic") or []
        if any(t.get("plan_id") for t in traffic_rows) or revision:
            serving = sum(float(t.get("percent") or 0) for t in traffic_rows
                          if t.get("plan_id") == data.get("id") or (revision and t.get("revision") == revision))
            health_body += f'<p><strong>This deployment receives {serving:g}% of traffic.</strong></p>'
        else:
            health_body += '<p class="d-caption">The serving version has not yet been matched to this deployment.</p>'
        for traffic in health.get("traffic") or []:
            share = max(0, min(100, float(traffic.get("percent") or 0)))
            health_body += (f'<div class="d-caption">{ui.esc(traffic.get("revision"))} · {share:g}% of traffic</div>'
                            f'<div class="d-traffic" role="img" aria-label="{share:g} percent of traffic"><span style="width:{share}%"></span></div>')
        app_url = health.get("url") or data.get("app_url")
        if app_url:
            health_body += link(app_url, "Open app ↗", True)
    else:
        health_body = (
            "<p>Live service status is not available yet.</p>"
            if setup or data.get("observer")
            else "<p>Live health monitoring is not connected. Configure the observer account to read Google Cloud status.</p>"
        )
    if data.get("health_error"):
        health_body += f'<div class="d-notice">{ui.esc(data["health_error"])} Last recorded status may be out of date.</div>'
    observation = ((data.get("phases") or {}).get("bake") or {}).get("result")
    observation_notes = {
        "inconclusive": "The startup checks passed, but there was too little traffic to judge real-world errors and response time.",
        "unobserved": "The deployment finished, but post-deployment monitoring could not read the app logs.",
        "skipped": "Post-deployment monitoring was turned off for this release. Review current health before relying on this result.",
    }
    if observation in observation_notes:
        health_body += f'<div class="d-notice"><strong>Monitoring note</strong><p>{observation_notes[observation]}</p></div>'
    if not setup:
        health_body += (
            '<div class="d-tools">'
            + link("/settings?" + urlencode({"tab": "health", "app": app, "env": "production" if data["environment"] == "Production" else "staging"}), "Health charts & recent errors")
            + "</div>"
        )
    health_panel = panel("Is the app working?", f'<div class="d-pad">{health_body}</div>')
    context = panel(
        "Deployment details",
        '<div class="d-pad">'
        + facts(
            [
                ("Application", app),
                ("Repository", data.get("repository")),
                ("Version", data.get("sha")),
                ("Change", data.get("message")),
                ("Google Cloud project", data.get("project")),
                ("Region", data.get("region")),
                ("Environment", data.get("environment")),
                ("Requested by", data.get("owner")),
                ("Started", moment(data.get("started_at"))),
                ("Finished", moment(data.get("finished_at"))),
                ("Deployment ID", data.get("id")),
                ("Workflow last checked", moment(data.get("observed_at"))),
            ]
        )
        + "</div>",
    )
    events = "".join(f"<li><time>{moment(e['at'], True)}</time><span>{ui.esc(e['text'])}</span></li>" for e in data.get("events") or [])
    activity = panel(
        "Activity",
        '<div class="d-pad"><ol class="d-activity">'
        + (events or "<li>No deployment events recorded yet.</li>")
        + '</ol><p class="d-caption">Times shown in UTC. Workflow details are available in the step panels and GitHub logs.</p></div>',
    )
    phases = data.get("phases") or {}
    reports = '<div class="d-pad">' + facts([
        ("Deployment result", label), ("Health check", data.get("checks", {}).get("health", "Not reported")),
        ("Deployed version", phases.get("revision")),
        ("Post-deployment observation",
         {"inconclusive": "Not enough traffic to judge", "unobserved": "Monitoring unavailable", "skipped": "Turned off"}.get(observation, observation or "Not reported")),
    ]) + '</div>'
    diagnosis = phases.get("diagnosis") or {}
    if diagnosis:
        reports += f'<div class="d-pad"><h3>What went wrong</h3><p>{ui.esc(diagnosis.get("cause"))}</p><p>{ui.esc(diagnosis.get("developer"))}</p></div>'
    for key, title in (("phases", "Deployment evidence & captured logs"), ("recovery", "Recovery status"), ("report", "Next-day comparison")):
        if data.get(key):
            reports += (
                f'<details class="d-job" id="report-{key}"><summary id="report-summary-{key}">{title}</summary><div class="d-pad"><pre>{ui.esc(json.dumps(data[key], indent=2))}</pre></div></details>'
            )
    if not data.get("report"):
        reports += '<div class="d-pad"><p>Next-day comparison: available after a successful deployment has been observed for a day, when monitoring is connected.</p></div>'
    reports = panel("Reports & evidence", reports, "Recorded evidence, kept with this deployment.")
    prior_setup = ""
    if data.get("setup"):
        prior_setup = '<details class="d-job" id="prior-setup"><summary id="prior-setup-summary">First-time setup completed</summary>' + workflow_groups(data["setup"]) + "</details>"
    journey = f'<div class="d-duo">{now_panel(data)}{visitors_panel(data)}</div>' + journey_panel(data) if stages else ""
    return head + notices + hero + journey + f'<div class="d-columns"><div>{workflow_groups(data)}{prior_setup}{checks}{activity}{reports}</div><aside>{health_panel}{context}</aside></div>'


def board_ring(done, total, tone_cls):
    fill = round(251.3 * done / total, 2) if total else 0
    return (
        f'<svg class="d-ring" viewBox="0 0 100 100" role="img" aria-label="{done} of {total} stages finished">'
        '<circle class="d-ring-track" cx="50" cy="50" r="40" fill="none" stroke-width="8"/>'
        f'<circle class="d-ring-fill" data-ring="stages" cx="50" cy="50" r="40" fill="none" stroke="#087f78" stroke-width="8" '
        f'style="stroke-dasharray:{fill} 251.3" transform="rotate(-90 50 50)" stroke-linecap="round"/>'
        f'<text x="50" y="52" text-anchor="middle">{f"{done}/{total}" if total else "—"}</text>'
        '<text class="d-ring-sub" x="50" y="66" text-anchor="middle">STAGES</text></svg>'
    )


def stage_hero(data, state, label, elapsed):
    """The top of a release's page: where it is, in one sentence, how long it has taken against how long it usually takes, and
    every stage on one line."""
    stages, now = data["stages"], data["now"]
    done = sum(stage["state"] in {"done", "skipped"} for stage in stages)
    current = next((stage for stage in stages if stage["state"] in {"running", "next"}), None)
    stopped = next((stage for stage in stages if stage["state"] in {"failed", "cancelled"}), None)
    cls = ""
    if stopped:
        sentence = f"Stopped at: {stopped['title']}."
        more = guide(stopped)[3] or stopped["text"]
        cls = "b-stop"
    elif current and not data.get("terminal"):
        sentence = f"Now: {current['title']}."
        more = current.get("note") or current["text"]
        cls = "b-now"
    elif state in {"succeeded", "rolled_back"} and data.get("terminal"):
        sentence, more = "Live.", ("Every stage passed, and the new version served everyone." if state == "succeeded" else
                                   "Every stage passed; later, visitors were sent back to the version before it.")
        cls = "b-live"
    elif state == "succeeded":
        sentence, more = "GitHub has finished.", "The platform is recording the result and the checks, usually within a minute."
        cls = "b-live"
    else:
        sentence, more = "", step_name(data.get("current")) or ""
    typical = data.get("typical")
    usual = (f"About {round(typical['seconds'] / 60) or 1} min" if typical else "Not enough history yet")
    usual_note = f"Median of its last {typical['count']} successful releases." if typical else "Shown after three successful releases."
    running = not data.get("terminal") and data.get("started_at")
    took = ticking(data["started_at"], now) if running else ui.esc(duration(elapsed))
    share = (data.get("cloud") or {}).get("share")
    if data.get("terminal") and state == "succeeded":
        share = 100.0
    visitors = f"{float(share):g}%" if share is not None else "0%"
    stats = (
        f'<div class="d-stat"><span class="d-caption">{"Elapsed time" if running else "Took"}</span><strong>{took}</strong></div>'
        f'<div class="d-stat"><span class="d-caption">Stages done</span><strong>{done} of {len(stages)}</strong></div>'
        f'<div class="d-stat"><span class="d-caption">Usually takes</span><strong>{ui.esc(usual)}</strong></div>'
        f'<div class="d-stat"><span class="d-caption">Visitors on the new version</span><strong>{visitors}</strong></div>'
    )
    pill_tone = tone(state) if state not in {"succeeded", "rolled_back"} or data.get("terminal") else "running"
    pill = f'<span class="d-pill {pill_tone}"><i aria-hidden="true"></i>{ui.esc(label)}</span>'
    return (f'<section class="d-board {cls}" data-now="{now:.0f}"><div class="d-board-head">{board_ring(done, len(stages), cls)}<div>{pill}<h2>{ui.esc(sentence or label)}</h2>'
            + (f'<p class="d-lead">{ui.esc(more)}</p>' if more else "")
            + f'</div></div><div class="d-stats">{stats}</div>{track(stages)}<p class="d-caption" style="margin-top:14px">{ui.esc(usual_note)} '
            'Each stage is lit by what GitHub or Google Cloud reports, never by a timer.</p></section>')


def listing(data):
    rows = data["rows"]
    active, needs = data["counts"]["active"], data["counts"]["attention"]
    head = (
        '<div class="d-top"><div><div class="d-eyebrow">Your delivery workspace</div><h1>Deployments</h1><p>Follow every app from first setup to a running release.</p></div><div class="d-tools">'
        + link("/settings?tab=connections", "Set up an app", True)
        + link("/settings?tab=releases", "History & trends")
        + "</div></div>"
    )
    stats = (
        '<div class="d-list-stats">'
        + "".join(f"<div><span>{name}</span><strong>{value}</strong></div>" for name, value in (("In progress / prepared", active), ("Needs attention", needs), ("Recent deployments", len(rows))))
        + "</div>"
    )
    items = []
    for setup in reversed(data["setups"]):
        href = "/settings?" + urlencode({"tab": "deployments", "onboarding": setup["application"]})
        items.append(
            f'<a class="d-run" href="{ui.esc(href)}"><div><strong>{ui.esc(setup["application"])}</strong>'
            f'<span class="d-caption">First-time setup · {ui.esc(setup["current"])}</span></div>{pill(setup["state"], setup["label"])}</a>'
        )
    for row in rows:
        items.append(
            f'<a class="d-run" href="{ui.esc(row["href"])}"><div><strong>{ui.esc(row["application"])}</strong>'
            f'<span class="d-caption">{ui.esc(row.get("summary") or row.get("kind"))} · {ui.esc(row["sha"][:8])}</span></div>{pill(row["state"], row["label"])}</a>'
        )
    return (
        head
        + stats
        + panel(
            "Recent activity",
            "".join(items) or '<div class="d-empty"><h2>Your first deployment will appear here</h2><p>Connect your accounts and check an app to get started.</p></div>',
            "Open a deployment to follow its live steps, checks, and report. Up to 100 recent deployments.",
        )
    )


def page(frame, data, query):
    endpoint = "/settings/deployment/status" + ("?" + urlencode(query) if query else "")
    body = detail(data) if query else listing(data)
    terminal = bool(query and data.get("terminal"))
    inner = (
        f'<style>{STYLE}</style><div class="deployment"><div class="d-tools" style="justify-content:space-between;margin-bottom:20px">'
        '<span class="d-live"><span class="d-dot" aria-hidden="true"></span><span id="deployment-connection">'
        + ("Finished · automatic updates off" if terminal else "Automatic updates every 10 seconds") + "</span></span>"
        + '<span class="d-tools">' + ('<button class="btn ghost" id="deployment-present" type="button">Full screen</button>' if query else "")
        + '<button class="btn ghost" id="deployment-refresh" type="button">Refresh now</button></span></div>'
        '<div id="deployment-announcement" class="d-announcement" role="status" aria-live="polite"></div>'
        f'<div id="deployment-content" data-state="{ui.esc(data.get("state", "list"))}" data-terminal="{str(terminal).lower()}" '
        f'data-endpoint="{ui.esc(endpoint)}">{body}</div>'
        "<noscript><p>Automatic updates need JavaScript. Reload this page to see the latest status.</p></noscript></div>"
        '<script src="/settings/deployment/script.js" defer></script>'
    )
    return ui.shell(
        frame, "deployments", inner, headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'"}, title="Deployments · Fast Deploy"
    )
