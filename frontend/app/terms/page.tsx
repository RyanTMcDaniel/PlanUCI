export const metadata = {
  title: "Terms of Service – PlanUCI",
};

export default function TermsPage() {
  return (
    <main className="min-h-screen bg-white px-6 py-16 text-gray-800">
      <div className="mx-auto max-w-2xl">
        <h1 className="text-3xl font-bold tracking-tight text-gray-900">
          Terms of Service
        </h1>
        <p className="mt-2 text-sm text-gray-500">Last updated: May 24, 2026</p>

        <Section title="Educational tool only">
          <p>
            PlanUCI is an independent, student-built tool intended to help UCI
            students explore course sequences and plan their academic careers.
            It is <strong>not affiliated with, endorsed by, or operated by</strong> the
            University of California, Irvine, or any of its departments,
            offices, or representatives.
          </p>
        </Section>

        <Section title="Not official academic guidance">
          <p>
            Nothing on PlanUCI constitutes official academic advising or
            counseling. Course requirements, prerequisite rules, GE
            requirements, and major requirements change over time and may differ
            from what is shown. Always verify your plan with your official UCI
            academic advisor and the{" "}
            <a
              href="https://catalogue.uci.edu"
              className="text-blue-600 underline hover:text-blue-700"
              target="_blank"
              rel="noopener noreferrer"
            >
              UCI General Catalogue
            </a>
            .
          </p>
        </Section>

        <Section title="No guarantee of accuracy">
          <p>
            PlanUCI makes no representations or warranties — express or implied
            — regarding the completeness, accuracy, or timeliness of any
            information displayed. Course offerings, availability, units, and
            prerequisites are sourced from third-party data and may be
            outdated or incorrect.
          </p>
        </Section>

        <Section title="Use at your own risk">
          <p>
            Your use of PlanUCI is entirely at your own risk. We are not
            liable for any academic, financial, or other consequences arising
            from decisions made based on information provided by this tool,
            including but not limited to course registration errors, missed
            requirements, or delayed graduation.
          </p>
        </Section>

        <Section title="Account and data">
          <p>
            You are responsible for maintaining the security of your Google
            account used to sign in. We reserve the right to suspend or
            terminate access for any user who misuses the service. See our{" "}
            <a href="/privacy" className="text-blue-600 underline hover:text-blue-700">
              Privacy Policy
            </a>{" "}
            for details on how your data is handled.
          </p>
        </Section>

        <Section title="Changes to these terms">
          <p>
            We may update these terms at any time. The date at the top of this
            page will reflect the most recent revision. Continued use of
            PlanUCI after changes are posted constitutes your acceptance of
            the revised terms.
          </p>
        </Section>

        <Section title="Contact">
          <p>
            Questions about these terms? Email us at{" "}
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
