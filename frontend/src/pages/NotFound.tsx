import { Compass } from 'lucide-react';
import { Link } from 'react-router-dom';
import { EmptyState } from '@/components/ui/empty-state';
import { buttonClasses } from '@/components/ui/button';

export default function NotFound() {
  return (
    <EmptyState
      icon={<Compass />}
      title="Page not found"
      description="The page you're looking for doesn't exist."
      action={
        <Link to="/" className={buttonClasses({ variant: 'primary' })}>
          Back to dashboard
        </Link>
      }
    />
  );
}
