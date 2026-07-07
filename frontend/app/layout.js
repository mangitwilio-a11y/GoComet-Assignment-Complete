import "./globals.css";

export const metadata = {
  title: "Nova · Trade-Doc Pipeline",
  description: "Multi-agent trade document extraction, validation, and routing",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
