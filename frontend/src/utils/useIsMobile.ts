import { useEffect, useState } from 'react';

// styles.css の @media (max-width: 768px) と揃えること。
export const MOBILE_MEDIA_QUERY = '(max-width: 768px)';

/** 画面幅がスマホ相当かどうか。左右のパネルを引き出し式に切り替えるために使う
 * （レイアウトの見た目だけなら CSS の @media で足りるが、パネルの開閉状態は
 * PC とスマホで初期値・閉じ方が異なるため JS 側でも判定する）。 */
export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(() => window.matchMedia(MOBILE_MEDIA_QUERY).matches);

  useEffect(() => {
    const media = window.matchMedia(MOBILE_MEDIA_QUERY);
    const onChange = () => setIsMobile(media.matches);
    onChange();
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, []);

  return isMobile;
}
