import { useId } from 'react';

/** The UGen mark: two lanes, local and cloud, meeting in one core (the gateway). */
function LogoMark() {
  const clip = useId();
  return <svg viewBox="0 0 48 48" aria-hidden="true" className="logo-mark">
    <defs><clipPath id={clip}><rect x="4" y="9" width="26" height="30" rx="13"/></clipPath></defs>
    <rect className="logo-local" x="4" y="9" width="26" height="30" rx="13"/>
    <rect className="logo-cloud" x="18" y="9" width="26" height="30" rx="13"/>
    <rect className="logo-core" x="18" y="9" width="26" height="30" rx="13" clipPath={`url(#${CSS.escape(clip)})`}/>
  </svg>;
}

export default LogoMark;
