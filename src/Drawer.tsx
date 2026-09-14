import {useEffect,useRef,type ReactNode} from 'react';
import {X} from 'lucide-react';
export default function Drawer({title,eyebrow,children,close}:{title:string;eyebrow:string;children:ReactNode;close:()=>void}){
 const dialog=useRef<HTMLDivElement>(null);
 const closeRef=useRef(close);closeRef.current=close;
 useEffect(()=>{const origin=document.activeElement as HTMLElement;const el=dialog.current!;el.querySelector<HTMLElement>('button')?.focus();const key=(e:KeyboardEvent)=>{if(e.key==='Escape')closeRef.current();if(e.key==='Tab'){const items=Array.from(el.querySelectorAll<HTMLElement>('button,a[href],input,select,textarea,[tabindex="0"]')).filter(n=>!(n as HTMLButtonElement).disabled);const first=items[0],last=items.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus()}}};document.addEventListener('keydown',key);return()=>{document.removeEventListener('keydown',key);if(origin?.isConnected)origin.focus()}},[]);
 return <><div className="drawer-backdrop" onClick={close}/><div ref={dialog} className="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title"><header className="drawer-head"><div><div className="drawer-eyebrow">{eyebrow}</div><h2 id="drawer-title">{title}</h2></div><button className="icon-button" onClick={close} aria-label="关闭详情"><X/></button></header><div className="drawer-body">{children}</div></div></>
}
