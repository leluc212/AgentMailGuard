import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import "./i18n";
import "./index.css";
import { Layout } from "./components/Layout";
import { GuardInspector } from "./screens/GuardInspector";
import { BenchmarkExplorer, Observability } from "./screens/Insights";
import { LivePipeline } from "./screens/LivePipeline";
import { Knowledge, Review, Timeline } from "./screens/Operations";
import { ScenarioRunner } from "./screens/ScenarioRunner";
import { ConsoleProvider } from "./state";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ConsoleProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<LivePipeline />} />
            <Route path="scenarios" element={<ScenarioRunner />} />
            <Route path="guard" element={<GuardInspector />} />
            <Route path="review" element={<Review />} />
            <Route path="timeline" element={<Timeline />} />
            <Route path="knowledge" element={<Knowledge />} />
            <Route path="observability" element={<Observability />} />
            <Route path="benchmark" element={<BenchmarkExplorer />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ConsoleProvider>
  </StrictMode>,
);
