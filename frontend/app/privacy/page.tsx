export const metadata = {
  title: "Privacy Policy – PlanUCI",
};

export default function PrivacyPage() {
  return (
    <main className="min-h-screen bg-white px-6 py-16 text-gray-800">
      <div className="mx-auto max-w-2xl">
        <h1 className="text-3xl font-bold tracking-tight text-gray-900">
          Privacy Policy
        </h1>
        <p className="mt-2 text-sm text-gray-500">Last updated: May 24, 2026</p>

        <Section title="What we collect">
          <p>
            PlanUCI collects only the information needed to provide its
            planning features:
          </p>
          <ul className="mt-3 list-disc space-y-1 pl-5">
            <li>Your Google account email address and display name (used for sign-in)</li>
            <li>Courses you add to your academic plans</li>
            <li>Saved schedules and planner state</li>
          </ul>
        </Section>

        <Section title="What we do not collect">
          <p>We never collect or store:</p>
          <ul className="mt-3 list-disc space-y-1 pl-5">
            <li>Passwords — authentication is handled entirely by Google</li>
            <li>Payment or financial information of any kind</li>
            <li>Social security numbers, government IDs, or other sensitive personal data</li>
            <li>Location data or device identifiers beyond what your browser sends by default</li>
          </ul>
        </Section>

        <Section title="How your data is stored">
          <p>
            All data is stored in{" "}
            <a
              href="https://supabase.com"
              className="text-blue-600 underline hover:text-blue-700"
              target="_blank"
              rel="noopener noreferrer"
            >
              Supabase
            </a>
            , a managed PostgreSQL platform with encryption at rest and in
            transit. Your data is never sold, shared with advertisers, or
            disclosed to third parties except as required by law.
          </p>
        </Section>

        <Section title="Deleting your data">
          <p>
            You can request deletion of your account and all associated data at
            any time by emailing{" "}
            <a
              href="mailto:rtmcdani@uci.edu"
              className="text-blue-600 underline hover:text-blue-700"
            >
              rtmcdani@uci.edu
            </a>
            . We will process your request within 30 days.
          </p>
        </Section>

        <Section title="Changes to this policy">
          <p>
            If this policy changes materially, we will update the date at the
            top of this page. Continued use of PlanUCI after a change
            constitutes acceptance of the updated policy.
          </p>
        </Section>

        <Section title="Contact">
          <p>
            Questions about this policy? Reach us at{" "}
            <a
              href="mailto:rtmcdani@uci.edu"
              className="text-blue-600 underline hover:text-blue-700"
            >
              rtmcdani@uci.edu
            </a>
            .
          </p>
        </Section>
      </div>
    </main>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mt-10">
      <h2 className="text-lg font-semibold text-gray-900">{title}</h2>
      <div className="mt-3 text-sm leading-relaxed text-gray-600">
        {children}
      </div>
    </section>
  );
}
