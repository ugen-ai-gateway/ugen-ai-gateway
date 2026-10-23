import type { User } from '../api';

/** The user's picture, or their initial when they have not set one. */
function Avatar({ user, className = '' }: { user: User; className?: string }) {
  return <span className={`avatar ${className}`} aria-hidden="true">
    {user.avatar ? <img src={user.avatar} alt=""/> : user.username.slice(0, 1).toUpperCase()}
  </span>;
}

export default Avatar;
