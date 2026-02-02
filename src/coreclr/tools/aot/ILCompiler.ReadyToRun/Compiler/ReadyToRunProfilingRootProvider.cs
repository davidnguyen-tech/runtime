// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using Internal.JitInterface;
using Internal.TypeSystem.Ecma;
using Internal.TypeSystem;

namespace ILCompiler
{
    /// <summary>
    /// Roots all methods in the profile data in the module.
    /// </summary>
    public class ReadyToRunProfilingRootProvider : ICompilationRootProvider
    {
        private EcmaModule _module;
        private IEnumerable<MethodDesc> _profileData;
        private InstructionSetSupport _instructionSetSupport;

        public ReadyToRunProfilingRootProvider(EcmaModule module, ProfileDataManager profileDataManager)
        {
            _module = module;
            _profileData = profileDataManager.GetInputProfileDataMethodsForModule(module);
            _instructionSetSupport = ((ReadyToRunCompilerContext)module.Context).InstructionSetSupport;
        }

        public void AddCompilationRoots(IRootingServiceProvider rootProvider)
        {
            int profileMethodCount = 0;
            int rootedMethodCount = 0;
            Dictionary<string, int> genericTypeUsage = new Dictionary<string, int>();
            
            foreach (var method in _profileData)
            {
                profileMethodCount++;
                try
                {
                    // Validate that this method is fully instantiated
                    if (method.OwningType.IsGenericDefinition || method.OwningType.ContainsSignatureVariables())
                    {
                        continue;
                    }

                    if (method.IsGenericMethodDefinition)
                    {
                        continue;
                    }

                    bool containsSignatureVariables = false;
                    foreach (TypeDesc t in method.Instantiation)
                    {
                        if (t.IsGenericDefinition)
                        {
                            containsSignatureVariables = true;
                            break;
                        }

                        if (t.ContainsSignatureVariables())
                        {
                            containsSignatureVariables = true;
                            break;
                        }
                    }
                    if (containsSignatureVariables)
                        continue;

                    if (!CorInfoImpl.ShouldSkipCompilation(_instructionSetSupport, method))
                    {
                        ReadyToRunLibraryRootProvider.CheckCanGenerateMethod(method);
                        rootProvider.AddCompilationRoot(method, rootMinimalDependencies: true, reason: "Profile triggered method");
                        rootedMethodCount++;
                        
                        // Log generic instantiations with their type arguments
                        if (method.HasInstantiation || method.OwningType.HasInstantiation)
                        {
                            StringBuilder sb = new StringBuilder();
                            sb.Append($"  {method.OwningType}");
                            
                            if (method.HasInstantiation)
                            {
                                sb.Append(".");
                                sb.Append(method.Name.ToString());
                                sb.Append("<");
                                for (int i = 0; i < method.Instantiation.Length; i++)
                                {
                                    if (i > 0) sb.Append(", ");
                                    sb.Append(method.Instantiation[i]);
                                }
                                sb.Append(">");
                            }
                            else
                            {
                                sb.Append(".");
                                sb.Append(method.Name.ToString());
                            }
                            
                            Console.WriteLine($"[DIAG]   Generic: {sb}");
                            
                            // Track type argument usage
                            if (method.OwningType.HasInstantiation)
                            {
                                for (int i = 0; i < method.OwningType.Instantiation.Length; i++)
                                {
                                    string typeName = method.OwningType.Instantiation[i].ToString();
                                    if (genericTypeUsage.TryGetValue(typeName, out int count))
                                        genericTypeUsage[typeName] = count + 1;
                                    else
                                        genericTypeUsage[typeName] = 1;
                                }
                            }
                            
                            if (method.HasInstantiation)
                            {
                                for (int i = 0; i < method.Instantiation.Length; i++)
                                {
                                    string typeName = method.Instantiation[i].ToString();
                                    if (genericTypeUsage.TryGetValue(typeName, out int count))
                                        genericTypeUsage[typeName] = count + 1;
                                    else
                                        genericTypeUsage[typeName] = 1;
                                }
                            }
                        }
                    }
                }
                catch (TypeSystemException)
                {
                    // Individual methods can fail to load types referenced in their signatures.
                    // Skip them in library mode since they're not going to be callable.
                    continue;
                }
            }
            Console.WriteLine($"[DIAG] ReadyToRunProfilingRootProvider for {_module.Assembly.GetName().Name}: {profileMethodCount} methods in profile, rooted {rootedMethodCount} methods");
            
            if (genericTypeUsage.Count > 0)
            {
                Console.WriteLine($"[DIAG] Type arguments used in generic instantiations:");
                foreach (var kvp in genericTypeUsage.OrderByDescending(x => x.Value))
                {
                    Console.WriteLine($"[DIAG]   {kvp.Key}: {kvp.Value} instantiations");
                }
            }
        }
    }
}
