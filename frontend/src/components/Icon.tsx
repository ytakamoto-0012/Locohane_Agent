type IconName =
  | 'paperclip'
  | 'folder'
  | 'gauge'
  | 'palette'
  | 'sun'
  | 'moon'
  | 'monitor'
  | 'plus'
  | 'x'
  | 'arrow-down'
  | 'play'
  | 'trash'
  | 'pencil'
  | 'panel-left'
  | 'panel-right'
  | 'menu'
  | 'user'
  | 'log-out';

function IconPath({ name }: { name: IconName }) {
  switch (name) {
    case 'paperclip':
      return (
        <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 17.07 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48" />
      );
    case 'folder':
      return <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />;
    case 'gauge':
      return <path d="M12 14l4-4M3.34 19a10 10 0 1 1 17.32 0" />;
    case 'palette':
      return (
        <>
          <path d="M12 2a10 10 0 1 0 0 20c1.1 0 2-.9 2-2 0-.5-.2-1-.5-1.4-.3-.4-.5-.8-.5-1.3 0-1.1.9-2 2-2h2.3c2.3 0 4.2-1.9 4.2-4.2C21.5 6 17.2 2 12 2z" />
          <circle cx="6.5" cy="11.5" r="1.3" />
          <circle cx="9.5" cy="7.3" r="1.3" />
          <circle cx="14.5" cy="7.3" r="1.3" />
          <circle cx="17.3" cy="11.5" r="1.3" />
        </>
      );
    case 'sun':
      return (
        <>
          <circle cx="12" cy="12" r="4.3" />
          <path d="M12 1.5v2.5M12 20v2.5M4.2 4.2l1.8 1.8M18 18l1.8 1.8M1.5 12h2.5M20 12h2.5M4.2 19.8l1.8-1.8M18 6l1.8-1.8" />
        </>
      );
    case 'moon':
      return <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z" />;
    case 'monitor':
      return (
        <>
          <rect x="2.5" y="3.5" width="19" height="13" rx="2" />
          <path d="M8 20.5h8M12 16.5v4" />
        </>
      );
    case 'plus':
      return <path d="M12 5v14M5 12h14" />;
    case 'x':
      return <path d="M18 6L6 18M6 6l12 12" />;
    case 'arrow-down':
      return <path d="M12 4v16M6 14l6 6 6-6" />;
    case 'play':
      return <path d="M7 4.5v15l12-7.5z" />;
    case 'trash':
      return (
        <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0-1 14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2L4 6h16zM10 11v6M14 11v6" />
      );
    case 'pencil':
      return (
        <path d="M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
      );
    case 'panel-left':
      return (
        <>
          <rect x="2.5" y="4" width="19" height="16" rx="2" />
          <path d="M9.5 4v16" />
        </>
      );
    case 'panel-right':
      return (
        <>
          <rect x="2.5" y="4" width="19" height="16" rx="2" />
          <path d="M14.5 4v16" />
        </>
      );
    case 'menu':
      return <path d="M3 6h18M3 12h18M3 18h18" />;
    case 'user':
      return (
        <>
          <circle cx="12" cy="8" r="4" />
          <path d="M4 21c0-4.4 3.6-8 8-8s8 3.6 8 8" />
        </>
      );
    case 'log-out':
      return (
        <>
          <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
          <path d="M16 17l5-5-5-5M21 12H9" />
        </>
      );
  }
}

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <IconPath name={name} />
    </svg>
  );
}
