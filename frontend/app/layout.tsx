import type { Metadata } from "next";
import { Geist } from "next/font/google";
import "./globals.css";
import Navbar from "./components/Navbar";

const geist = Geist({ subsets: ["latin"], variable: "--font-geist-sans" });

export const metadata: Metadata = {
  title: "PlanUCI",
  description: "Plan your UCI academic career.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={`${geist.variable} h-full`}>
      <body className="h-full flex flex-col bg-[#0f0f0f] text-zinc-100 antialiased">
        <Navbar />
        {children}
      </body>
    </html>
  );
}
